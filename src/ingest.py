"""Stage 1 - Ingest. Pull every source into data/raw/ exactly as received, and prove completeness.

Retrieval modes: REST/JSON APIs (OpenSky, AviationStack, Open-Meteo), a CSV file (OurAirports),
and an official published PDF report (DGCA). Raw files are never edited after download.
Each one gets a manifest entry (URL, retrieved-at, rows, sha256) in data/raw/_manifest.json.

Rerun behaviour: a raw file that already exists is reused (no network, no credential needed),
so the committed raw snapshot reproduces every output offline. `--refresh` re-downloads, writing
to a temp file first so a failed refresh never destroys the last good copy.

AviationStack is NEVER called by a normal run. Its free tier is 100 calls/month, so calls are spent
only through an explicit `--aviationstack-calls N`, and a persistent ledger enforces a hard cap.

    python src/ingest.py                         # fill anything missing, reuse the rest
    python src/ingest.py --refresh weather,dgca  # re-pull specific sources
    python src/ingest.py --aviationstack-calls 8 # spend up to 8 AviationStack calls (ledger-capped)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import statistics
import time
from datetime import date, datetime, timezone
from urllib.parse import quote

import pandas as pd
import pdfplumber

from common import (PROCESSED, RAW, CredentialMissing, SourceError, atomic_write, day_bounds_epoch,
                    get_logger, http_request, load_config, local_days, record_raw,
                    require_env)

log = get_logger("ingest")
ALL_SOURCES = ("ourairports", "weather", "dgca", "opensky")


def endpoint(url: str, source: str) -> str:
    """Fault injection for the failure-handling test: SKYPULSE_FAULT=weather points that source at a dead host."""
    faults = {s.strip() for s in os.environ.get("SKYPULSE_FAULT", "").split(",") if s.strip()}
    if source in faults:
        log.warning("FAULT INJECTION active for %s: using an unreachable endpoint", source)
        return "https://fault-injection.invalid/"
    return url


def cached(path, refresh: bool) -> bool:
    return path.exists() and not refresh


# ---------------------------------------------------------------- 4. OurAirports (CSV file)
def ingest_ourairports(cfg: dict, refresh: bool) -> dict:
    src = cfg["sources"]["ourairports"]
    path = RAW / "ourairports" / "airports.csv"
    if not cached(path, refresh):
        r = http_request("GET", endpoint(src["url"], "ourairports"), log=log, label="ourairports", cfg=cfg)
        atomic_write(path, r.content)
        df = pd.read_csv(path, keep_default_na=False, dtype=str)
        record_raw(path, source="ourairports", url=src["url"], params=None, rows=len(df))
    df = pd.read_csv(path, keep_default_na=False, dtype=str)
    rows = len(df)
    if rows < src["min_rows"]:
        raise SourceError(f"ourairports: only {rows} rows (expected >= {src['min_rows']}); file looks truncated")
    if cfg["airport"]["icao"] not in set(df["ident"]):
        raise SourceError(f"ourairports: {cfg['airport']['icao']} missing from airports.csv")
    log.info("ourairports: %d rows (floor %d), %s present", rows, src["min_rows"], cfg["airport"]["icao"])
    return {"source": "ourairports", "files": 1, "rows": rows, "expected": f">= {src['min_rows']}", "status": "ok"}


# ---------------------------------------------------------------- 3. Open-Meteo (JSON API)
def ingest_weather(cfg: dict, refresh: bool) -> dict:
    src, ap, w = cfg["sources"]["open_meteo"], cfg["airport"], cfg["analysis_window"]
    path = RAW / "weather" / f"open_meteo_{ap['icao']}_{w['start']}_{w['end']}.json"
    expected = 24 * len(local_days(cfg))
    if not cached(path, refresh):
        params = {"latitude": ap["lat"], "longitude": ap["lon"], "start_date": w["start"], "end_date": w["end"],
                  "hourly": ",".join(src["hourly"]), "timezone": ap["timezone"]}
        r = http_request("GET", endpoint(src["base_url"], "weather"), params=params, log=log, label="open-meteo", cfg=cfg)
        payload = r.json()
        n = len(payload.get("hourly", {}).get("time", []))
        if n == 0:
            raise SourceError("open-meteo: response has no hourly rows")
        atomic_write(path, json.dumps(payload))
        record_raw(path, source="open_meteo", url=src["base_url"], params=params, rows=n,
                   status="ok" if n == expected else "partial")
    hourly = json.loads(path.read_text())["hourly"]
    n = len(hourly["time"])
    nulls = {k: sum(v is None for v in vals) for k, vals in hourly.items() if k != "time"}
    status = "ok" if n == expected else "partial"
    log.info("open-meteo: %d hourly rows (expected %d) status=%s nulls=%s", n, expected, status, nulls)
    if status != "ok":
        raise SourceError(f"open-meteo: {n} hourly rows but window needs {expected}; weather join would be incomplete")
    return {"source": "open_meteo", "files": 1, "rows": n, "expected": expected, "status": status}


# ---------------------------------------------------------------- 5. DGCA (official PDF report)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def dgca_month_pattern(month: str) -> re.Pattern:
    y, m = month.split("-")
    name = MONTHS[int(m) - 1]
    # DGCA's own file naming is inconsistent: "traffic Data August 26.pdf", "TrafficDataMar26.pdf",
    # "TrafficReportApril2026.pdf". Accept full or 3-letter month, optional space, 2- or 4-digit year.
    return re.compile(rf"(?i)({name}|{name[:3]})\s*(20)?{y[2:]}\b")


def ingest_dgca(cfg: dict, refresh: bool) -> dict:
    src = cfg["sources"]["dgca"]
    month = src["benchmark_month"]
    path = RAW / "dgca" / f"dgca_traffic_report_{month}.pdf"
    if not cached(path, refresh):
        form = {"baseLocale": "", "screenId": "10000001", "classification": "", "actionVal": "viewStaticData",
                "requestType": "ApplicationRH", "attachId": "", "langType": "1", "attr": "",
                "ruleBookId": src["listing_rulebook_id"], "contentId": src["listing_content_id"],
                "serviceName": "fetchRulebookContentDtlsList"}
        r = http_request("POST", endpoint(src["listing_url"], "dgca"), data=form, log=log, label="dgca-listing", cfg=cfg)
        listing_path = RAW / "dgca" / f"dgca_air_traffic_listing_{datetime.now(timezone.utc):%Y-%m-%d}.json"
        atomic_write(listing_path, r.text)
        links = re.findall(r'data-url=\\?"([^"\\]*airTraffic[^"\\]*\.pdf)', r.text)
        record_raw(listing_path, source="dgca", url=src["listing_url"], params=form, rows=len(links),
                   note="DGCA 'Air Traffic' page listing (monthly domestic traffic reports)")
        pat = dgca_month_pattern(month)
        hits = [l for l in links if pat.search(l.rsplit("/", 1)[-1])]
        log.info("dgca: listing has %d traffic reports; %d match %s: %s", len(links), len(hits), month, hits)
        if not hits:
            raise SourceError(f"dgca: no traffic report for {month} on the DGCA listing yet "
                              f"(set sources.dgca.benchmark_month to a published month)")
        file_url = src["file_host"] + quote(hits[0].replace("jsp/dgca", "", 1))
        pdf = http_request("GET", endpoint(file_url, "dgca"), log=log, label="dgca-pdf", cfg=cfg)
        if not pdf.content.startswith(b"%PDF"):
            raise SourceError("dgca: downloaded file is not a PDF")
        atomic_write(path, pdf.content)
        with pdfplumber.open(io.BytesIO(pdf.content)) as p:
            pages = len(p.pages)
        record_raw(path, source="dgca", url=file_url, params=None, rows=pages,
                   note=f"rows = PDF pages. Monthly domestic traffic report, {month}")
    with pdfplumber.open(path) as p:
        pages = len(p.pages)
        has_otp = any("On-Time Performance" in (pg.extract_text() or "") for pg in p.pages)
    if not has_otp:
        raise SourceError("dgca: report has no 'On-Time Performance' section; format changed?")
    log.info("dgca: %s, %d pages, OTP section present", path.name, pages)
    return {"source": "dgca", "files": 1, "rows": pages, "expected": "OTP section present", "status": "ok"}


# ---------------------------------------------------------------- 1. OpenSky (JSON API, OAuth2)
class OpenSkyClient:
    def __init__(self, cfg: dict):
        self.cfg, self.src = cfg, cfg["sources"]["opensky"]
        self.token, self.expires = None, 0.0

    def headers(self) -> dict:
        if time.time() > self.expires - 60:
            creds = require_env("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET")
            r = http_request("POST", endpoint(self.src["token_url"], "opensky"), log=log, label="opensky-auth", cfg=self.cfg,
                             data={"grant_type": "client_credentials", "client_id": creds["OPENSKY_CLIENT_ID"],
                                   "client_secret": creds["OPENSKY_CLIENT_SECRET"]})
            tok = r.json()
            self.token, self.expires = tok["access_token"], time.time() + int(tok.get("expires_in", 1800))
            log.info("opensky: OAuth2 token acquired (valid %ss)", tok.get("expires_in"))
        return {"Authorization": f"Bearer {self.token}"}


def ingest_opensky(cfg: dict, refresh: bool) -> dict:
    src, icao = cfg["sources"]["opensky"], cfg["airport"]["icao"]
    client = OpenSkyClient(cfg)
    counts: dict[str, dict[str, int]] = {"departure": {}, "arrival": {}}
    fetched = 0
    window = local_days(cfg)
    crosscheck = [date.fromisoformat(d) for d in cfg.get("crosscheck_days", [])]
    for direction in ("departure", "arrival"):
        for day in window + crosscheck:
            path = RAW / "opensky" / direction / f"{icao}_{day}.json"
            if not cached(path, refresh):
                begin, end = day_bounds_epoch(day, cfg)
                params = {"airport": icao, "begin": begin, "end": end - 1}  # end-1: no double-count at midnight
                url = endpoint(f"{src['base_url']}/flights/{direction}", "opensky")
                r = http_request("GET", url, params=params, headers=client.headers(), log=log,
                                 label=f"opensky-{direction}-{day}", empty_statuses=(404,), cfg=cfg)
                flights = [] if r is None else r.json()
                atomic_write(path, json.dumps(flights))
                record_raw(path, source="opensky", url=url, params=params, rows=len(flights),
                           status="ok" if flights else "empty")
                fetched += 1
                log.info("opensky: %s %s -> %d legs (credits left today: %s)", direction, day, len(flights),
                         r.headers.get("X-Rate-Limit-Remaining", "?") if r is not None else "?")
                time.sleep(0.5)  # be polite to a free community service
            n = len(json.loads(path.read_text()))
            if day in crosscheck:
                log.info("opensky: cross-check day %s %s -> %d legs", direction, day, n)
                continue
            counts[direction][str(day)] = n
    # Completeness: a day far below the window's typical volume is a partial pull, not a quiet day.
    flags = []
    for direction, per_day in counts.items():
        med = statistics.median(per_day.values()) if per_day else 0
        floor = src["expected_min_daily_departures"]
        for day, n in per_day.items():
            reasons = []
            if n < floor:
                reasons.append(f"below absolute floor {floor}")
            if med and n < src["partial_day_ratio"] * med:
                reasons.append(f"< {src['partial_day_ratio']:.0%} of window median {med:.0f}")
            if reasons:
                flags.append({"direction": direction, "day": day, "rows": n, "reason": "; ".join(reasons)})
    comp = pd.DataFrame([{"direction": d, "local_day": day, "rows": n} for d, per in counts.items() for day, n in per.items()])
    PROCESSED.mkdir(parents=True, exist_ok=True)
    comp.to_csv(PROCESSED / "opensky_daily_counts.csv", index=False)
    total = sum(sum(v.values()) for v in counts.values())
    for f in flags:
        log.warning("opensky: PARTIAL-PULL FLAG %s %s rows=%d (%s)", f["direction"], f["day"], f["rows"], f["reason"])
    log.info("opensky: %d files (%d fetched now), %d legs; departures=%d arrivals=%d; %d day(s) flagged",
             len(comp), fetched, total, sum(counts["departure"].values()), sum(counts["arrival"].values()), len(flags))
    return {"source": "opensky", "files": len(comp), "rows": total,
            "expected": f"{len(local_days(cfg))} days x 2 directions", "status": "ok" if not flags else "flagged",
            "flags": flags}


# ---------------------------------------------------------------- 2. AviationStack (JSON API, rationed)
LEDGER = RAW / "aviationstack" / "_call_ledger.json"


def load_ledger(cap: int) -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"cap": cap, "calls": []}


def sample_aviationstack(cfg: dict, n_calls: int) -> dict:
    """Spend up to n_calls: for each direction x airline filter, page by 100 until the result set is exhausted.

    Departures are paged before arrivals (the KPI is a departure metric). Ledger-capped.
    """
    src, iata = cfg["sources"]["aviationstack"], cfg["airport"]["iata"]
    key = require_env("AVIATIONSTACK_ACCESS_KEY")["AVIATIONSTACK_ACCESS_KEY"]
    ledger = load_ledger(src["max_calls_total"])
    ledger["cap"] = src["max_calls_total"]
    used = len(ledger["calls"])
    budget = min(n_calls, ledger["cap"] - used)
    log.info("aviationstack: ledger shows %d/%d calls used; this run may spend %d", used, ledger["cap"], budget)
    if budget <= 0:
        log.warning("aviationstack: HARD CAP reached (%d/%d); no call made", used, ledger["cap"])
        return {"source": "aviationstack", "files": 0, "rows": 0, "expected": "n/a", "status": "cap_reached"}
    tasks = [(d, a) for d in ("dep_iata", "arr_iata") for a in src["airline_filters"]]
    coverage: dict[str, dict] = {}
    rows_total, spent = 0, 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for direction, airline in tasks:
        offset, total = 0, None
        while (total is None or offset < total) and spent < budget:
            # Resume: a page already fetched today (same UTC date) is reused from disk, not re-bought.
            done = sorted((RAW / "aviationstack").glob(
                f"{direction.split('_')[0]}_{iata}_{airline}_{stamp[:8]}T*_off{offset:04d}.json"))
            if done:
                body = json.loads(done[-1].read_text())
                total = body.get("pagination", {}).get("total", 0)
                log.info("aviationstack: reuse %s (no call)", done[-1].name)
                offset += src["page_size"]
                continue
            params = {"access_key": key, direction: iata, "airline_iata": airline,
                      "limit": src["page_size"], "offset": offset}
            # Log the call to the ledger BEFORE making it: a crash mid-call still counts against the budget.
            entry = {"n": len(ledger["calls"]) + 1, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "params": {k: v for k, v in params.items() if k != "access_key"}, "status": "started"}
            ledger["calls"].append(entry)
            atomic_write(LEDGER, json.dumps(ledger, indent=2))
            spent += 1
            label = f"aviationstack-{direction}-{airline}-off{offset}"
            try:
                # retries=1: every retry would be another paid call against the monthly quota.
                r = http_request("GET", endpoint(src["base_url"], "aviationstack"), params=params, log=log,
                                 label=label, cfg={**cfg, "http": {**cfg["http"], "retries": 1}})
                body = r.json()
                if "error" in body:
                    raise SourceError(f"aviationstack: API error {body['error']}")
            except SourceError as e:
                entry["status"] = f"failed: {e}"[:300]
                atomic_write(LEDGER, json.dumps(ledger, indent=2))
                raise
            data = body.get("data", [])
            total = body.get("pagination", {}).get("total", len(data))
            path = RAW / "aviationstack" / f"{direction.split('_')[0]}_{iata}_{airline}_{stamp}_off{offset:04d}.json"
            atomic_write(path, json.dumps(body))
            record_raw(path, source="aviationstack", url=src["base_url"], params=params, rows=len(data),
                       note=f"pagination total={total}")
            entry.update(status="ok", rows=len(data), pagination_total=total, file=path.name)
            atomic_write(LEDGER, json.dumps(ledger, indent=2))
            log.info("aviationstack: call %d/%d %s %s offset=%d -> %d rows (total %s)",
                     entry["n"], ledger["cap"], direction, airline, offset, len(data), total)
            rows_total += len(data)
            offset += src["page_size"]
            if not data:
                break
        got = min(offset, total or 0)
        coverage[f"{direction}:{airline}"] = {"retrieved": got, "total": total, "complete": total is not None and offset >= total}
    for k, c in coverage.items():
        (log.info if c["complete"] else log.warning)("aviationstack coverage %s: %s/%s %s", k, c["retrieved"], c["total"],
                                                     "complete" if c["complete"] else "INCOMPLETE (budget)")
    log.info("aviationstack: run spent %d call(s); ledger now %d/%d", spent, len(ledger["calls"]), ledger["cap"])
    return {"source": "aviationstack", "files": spent, "rows": rows_total, "expected": coverage,
            "status": "ok" if all(c["complete"] for c in coverage.values()) else "partial"}


def aviationstack_summary(cfg: dict) -> dict:
    files = sorted((RAW / "aviationstack").glob("*_off*.json"))
    rows = sum(len(json.loads(f.read_text()).get("data", [])) for f in files)
    ledger = load_ledger(cfg["sources"]["aviationstack"]["max_calls_total"])
    log.info("aviationstack: %d snapshot page(s) on disk, %d rows; ledger %d/%d calls used",
             len(files), rows, len(ledger["calls"]), ledger["cap"])
    return {"source": "aviationstack", "files": len(files), "rows": rows,
            "expected": f"ledger {len(ledger['calls'])}/{ledger['cap']}", "status": "ok" if files else "no_snapshots"}


# ---------------------------------------------------------------- orchestration
RUNNERS = {"ourairports": ingest_ourairports, "weather": ingest_weather, "dgca": ingest_dgca, "opensky": ingest_opensky}


def run(sources=ALL_SOURCES, refresh: set[str] | None = None, aviationstack_calls: int = 0) -> list[dict]:
    cfg = load_config()
    refresh = refresh or set()
    summaries = []
    for s in sources:
        log.info("---- ingest %s", s)
        summaries.append(RUNNERS[s](cfg, s in refresh or "all" in refresh))
    if aviationstack_calls > 0:
        summaries.append(sample_aviationstack(cfg, aviationstack_calls))
    else:
        summaries.append(aviationstack_summary(cfg))
    PROCESSED.mkdir(parents=True, exist_ok=True)
    atomic_write(PROCESSED / "ingest_summary.json", json.dumps(summaries, indent=2, default=str))
    return summaries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default=",".join(ALL_SOURCES), help="comma list of: " + ", ".join(ALL_SOURCES))
    ap.add_argument("--refresh", default="", help="comma list of sources to re-download, or 'all'")
    ap.add_argument("--aviationstack-calls", type=int, default=0, help="AviationStack calls to spend (default 0)")
    a = ap.parse_args()
    try:
        run([s for s in a.sources.split(",") if s], {s for s in a.refresh.split(",") if s}, a.aviationstack_calls)
    except (SourceError, CredentialMissing) as e:
        log.error("INGEST FAILED: %s", e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
