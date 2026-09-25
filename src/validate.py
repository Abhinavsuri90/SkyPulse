"""Stage 2 - Validate. Profile every source, stage it into SQLite, and FLAG (never delete) rule breaches.

Each rule has an ID, a business reason, and a severity:
  critical -> the source is unusable; the pipeline stops loudly
  exclude  -> the row stays in the database but is kept out of KPI calculations
  warn/info-> the row is kept and used; the flag documents a caveat

Outputs
  SQLite  stg_* tables + validation_flag (one row per rule hit)
  output/validation_report.md             profiling + rule results
  docs/known_unknown_assumptions.md       AUTO block (numbers regenerated every run)

    python src/validate.py
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from common import (DB_PATH, DOCS, OUTPUT, PROCESSED, RAW, SourceError, atomic_write, get_logger,
                    load_config, local_days)

log = get_logger("validate")

# ID -> (severity, description, business reason)
RULES = {
    # OpenSky flight legs
    "V-OS-1": ("exclude", "icao24 is a 6-char lowercase hex address", "Aircraft identity is the join key for turnarounds"),
    "V-OS-2": ("exclude", "lastSeen after firstSeen", "A leg cannot end before it starts"),
    "V-OS-3": ("exclude", "no duplicate leg (direction, icao24, firstSeen)", "Duplicates would double-count flights"),
    "V-OS-4": ("exclude", "callsign follows the airline pattern AAA9...", "Private / blank callsigns are not scheduled flights and have no schedule"),
    "V-OS-5": ("info", "callsign prefix is in the config airline map", "Unmapped airlines cannot be rolled up to a DGCA group"),
    "V-OS-6": ("exclude", "first/last detection within 20 km and 1,500 m of BLR", "A far detection means the timestamp is minutes off the real take-off/landing"),
    "V-OS-7": ("exclude", "not a BLR->BLR leg (circuit, or round trip merged by OpenSky)", "Merged round trips carry the OUTBOUND callsign on the inbound end"),
    "V-OS-8": ("info", "other-end airport is known", "Needed for domestic/international split; falls back to the schedule"),
    # AviationStack schedule sample
    "V-AS-1": ("exclude", "row is the operating flight, not a codeshare duplicate", "Codeshares repeat one physical flight under partner codes"),
    "V-AS-2": ("exclude", "one row per (direction, flight_date, operating flight)", "Paging a live feed can return the same flight twice"),
    "V-AS-3": ("info", "icao24 normalised to lowercase", "AviationStack sends UPPERCASE, OpenSky lowercase: a case-sensitive join silently loses ~70% of matches"),
    "V-AS-4": ("info", "'+00:00' timestamps re-read as local IST", "AviationStack labels local times as UTC (proven in model.py cross-check)"),
    "V-AS-5": ("exclude", "scheduled time present", "No schedule = nothing to measure delay against"),
    "V-AS-6": ("info", "flight not cancelled", "Cancelled flights keep their schedule but have no actual time"),
    # Weather
    "V-WX-1": ("critical", "exactly 24 hourly rows per day in the window", "Missing hours would silently under-count weather exposure"),
    "V-WX-2": ("critical", "no null values in the adverse-weather variables", "A null hour cannot be classified"),
    "V-WX-3": ("exclude", "values physically plausible (precip>=0, gust 0-250 km/h, visibility>=0)", "Sensor/model glitches must not trigger 'adverse'"),
    # Airports reference
    "V-AP-1": ("critical", "BLR present as VOBL / BLR / IN", "The whole project is anchored on this row"),
    "V-AP-2": ("info", "airport codes seen in flight legs resolve in airports.csv", "Unresolved codes cannot be classified domestic/international"),
    # Derived (applied in model.py)
    "V-DR-1": ("exclude", "leg matched to a schedule (flight code or learned callsign mapping)", "No schedule, no delay; the leg is still counted in traffic and turnarounds"),
    "V-DR-2": ("exclude", "computed delay within [-60, +720] min", "Outside this, the schedule match is almost certainly the wrong rotation/day"),
    "V-DR-3": ("info", "callsign->flight mapping is unambiguous", "Ambiguous mappings are not used"),
    "V-DR-4": ("info", "turnaround between 20 min and 12 h", "Shorter = artefact; longer = parked overnight, not an operational turn"),
    "V-DR-5": ("exclude", "flight code is not systematically offset from its schedule (late on >=80% of >=5 ops with a median > 60 min, or a stable offset while the Sept sample shows it on time; or early the same way)",
               "That signature is a schedule change between August and the Sept sample (a retime), not a delay"),
}

CALLSIGN_RE = r"[A-Z]{3}[0-9][0-9A-Z]{0,4}"


class Flags:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, entity: str, ids, rule: str, detail: str = ""):
        sev = RULES[rule][0]
        self.rows.extend({"entity": entity, "entity_id": i, "rule": rule, "severity": sev, "detail": detail} for i in ids)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=["entity", "entity_id", "rule", "severity", "detail"])


# ---------------------------------------------------------------- staging loaders
def stage_opensky(cfg: dict) -> pd.DataFrame:
    frames = []
    for direction in ("departure", "arrival"):
        for f in sorted(glob.glob(str(RAW / "opensky" / direction / "*.json"))):
            df = pd.DataFrame(json.loads(Path(f).read_text()))
            if df.empty:
                continue
            df["direction"] = "DEP" if direction == "departure" else "ARR"
            df["source_file"] = f.split("/raw/")[-1]
            df["local_day"] = f.rsplit("_", 1)[-1][:10]
            frames.append(df)
    o = pd.concat(frames, ignore_index=True)
    window = {str(d) for d in local_days(cfg)}
    o["is_crosscheck"] = (~o.local_day.isin(window)).astype(int)
    o["callsign"] = o["callsign"].fillna("").str.strip()
    o["leg_id"] = o.direction + "-" + o.icao24 + "-" + o.firstSeen.astype(str)
    return o.rename(columns={"firstSeen": "first_seen", "lastSeen": "last_seen",
                             "estDepartureAirport": "est_dep_airport", "estArrivalAirport": "est_arr_airport",
                             "estDepartureAirportHorizDistance": "dep_horiz_m", "estDepartureAirportVertDistance": "dep_vert_m",
                             "estArrivalAirportHorizDistance": "arr_horiz_m", "estArrivalAirportVertDistance": "arr_vert_m"})[
        ["leg_id", "direction", "icao24", "callsign", "first_seen", "last_seen", "est_dep_airport", "est_arr_airport",
         "dep_horiz_m", "dep_vert_m", "arr_horiz_m", "arr_vert_m", "local_day", "is_crosscheck", "source_file"]]


def _local_naive(ts: str | None) -> str | None:
    """'2026-09-22T18:00:00+00:00' -> '2026-09-22T18:00:00' (the '+00:00' label is wrong; the clock time is local)."""
    return ts[:19] if ts else None


def stage_aviationstack() -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(RAW / "aviationstack" / "*_off*.json"))):
        name = f.rsplit("/", 1)[-1]
        direction = "DEP" if name.startswith("dep") else "ARR"
        for i, x in enumerate(json.loads(Path(f).read_text()).get("data", [])):
            fl, cs = x.get("flight") or {}, (x.get("flight") or {}).get("codeshared")
            here, there = (x["departure"], x["arrival"]) if direction == "DEP" else (x["arrival"], x["departure"])
            icao24 = (x.get("aircraft") or {}).get("icao24")
            rows.append(dict(
                as_id=f"{name}#{i}", direction=direction, flight_date=x.get("flight_date"), status=x.get("flight_status"),
                flight_icao=fl.get("icao"), flight_iata=fl.get("iata"), is_codeshare=int(bool(cs)),
                op_flight_icao=(cs.get("flight_icao") or "").upper() if cs else fl.get("icao"),
                airline_icao=(x.get("airline") or {}).get("icao"),
                sched_local=_local_naive(here.get("scheduled")), actual_local=_local_naive(here.get("actual")),
                actual_runway_local=_local_naive(here.get("actual_runway")),
                raw_scheduled=here.get("scheduled"), other_icao=there.get("icao"), other_iata=there.get("iata"),
                icao24_raw=icao24, icao24=icao24.lower() if icao24 else None, source_file=f.split("/raw/")[-1]))
    return pd.DataFrame(rows)


def stage_weather(cfg: dict) -> pd.DataFrame:
    w = cfg["analysis_window"]
    payload = json.loads((RAW / "weather" / f"open_meteo_{cfg['airport']['icao']}_{w['start']}_{w['end']}.json").read_text())
    return pd.DataFrame(payload["hourly"]).rename(columns={"time": "hour_local"})


def stage_airports() -> pd.DataFrame:
    return pd.read_csv(RAW / "ourairports" / "airports.csv", keep_default_na=False, dtype=str)


# ---------------------------------------------------------------- rules
def validate_opensky(o: pd.DataFrame, cfg: dict, flags: Flags) -> None:
    E = "opensky_leg"
    flags.add(E, o.leg_id[~o.icao24.str.fullmatch(r"[0-9a-f]{6}")], "V-OS-1")
    flags.add(E, o.leg_id[o.last_seen <= o.first_seen], "V-OS-2")
    flags.add(E, o.leg_id[o.duplicated(["direction", "icao24", "first_seen"])], "V-OS-3")
    bad_cs = ~o.callsign.str.fullmatch(CALLSIGN_RE)
    flags.add(E, o.leg_id[bad_cs], "V-OS-4", "blank or registration-style callsign")
    flags.add(E, o.leg_id[~bad_cs & ~o.callsign.str[:3].isin(cfg["airlines"].keys())], "V-OS-5")
    dep, arr = o.direction == "DEP", o.direction == "ARR"
    far = (dep & ((o.dep_horiz_m > 20000) | (o.dep_vert_m > 1500))) | (arr & ((o.arr_horiz_m > 20000) | (o.arr_vert_m > 1500)))
    flags.add(E, o.leg_id[far], "V-OS-6")
    icao = cfg["airport"]["icao"]
    same = (o.est_dep_airport == icao) & (o.est_arr_airport == icao)
    dur = (o.last_seen - o.first_seen) / 60
    # Circuit / return-to-base (<60 min): neither end is a scheduled movement.
    flags.add(E, o.leg_id[same & (dur < 60)], "V-OS-7", "circuit / return to base")
    # Merged round trip (>=60 min): the BLR take-off is real, but the BLR landing carries the outbound callsign.
    flags.add(E, o.leg_id[same & (dur >= 60) & arr], "V-OS-7", "merged round trip: inbound end has outbound callsign")
    other = o.est_arr_airport.where(dep, o.est_dep_airport)
    flags.add(E, o.leg_id[other.isna()], "V-OS-8")


def validate_aviationstack(a: pd.DataFrame, flags: Flags) -> None:
    E = "aviationstack_row"
    flags.add(E, a.as_id[a.is_codeshare == 1], "V-AS-1")
    own = a[a.is_codeshare == 0]
    flags.add(E, own.as_id[own.duplicated(["direction", "flight_date", "op_flight_icao"])], "V-AS-2")
    flags.add(E, a.as_id[a.icao24_raw.notna() & (a.icao24_raw != a.icao24)], "V-AS-3")
    flags.add(E, a.as_id[a.raw_scheduled.fillna("").str.endswith("+00:00")], "V-AS-4")
    flags.add(E, a.as_id[a.sched_local.isna()], "V-AS-5")
    flags.add(E, a.as_id[a.status == "cancelled"], "V-AS-6")


def validate_weather(w: pd.DataFrame, cfg: dict, flags: Flags) -> None:
    expected = 24 * len(local_days(cfg))
    used = ["precipitation", "wind_gusts_10m", "visibility", "weather_code"]
    if len(w) != expected or w.hour_local.duplicated().any():
        flags.add("weather_hour", ["ALL"], "V-WX-1", f"{len(w)} rows, expected {expected}")
    nulls = w[used].isna().any(axis=1)
    if nulls.any():
        flags.add("weather_hour", w.hour_local[nulls], "V-WX-2")
    bad = (w.precipitation < 0) | ~w.wind_gusts_10m.between(0, 250) | (w.visibility < 0)
    flags.add("weather_hour", w.hour_local[bad], "V-WX-3")


def validate_airports(ap: pd.DataFrame, o: pd.DataFrame, cfg: dict, flags: Flags) -> None:
    blr = ap[ap.ident == cfg["airport"]["icao"]]
    if len(blr) != 1 or blr.iloc[0].iata_code != cfg["airport"]["iata"] or blr.iloc[0].iso_country != cfg["airport"]["country"]:
        flags.add("airport", [cfg["airport"]["icao"]], "V-AP-1")
    known = set(ap.ident) | set(ap.icao_code) | set(ap.gps_code)
    codes = pd.concat([o.est_dep_airport, o.est_arr_airport]).dropna().unique()
    flags.add("airport", [c for c in codes if c not in known], "V-AP-2")


# ---------------------------------------------------------------- profiling
def profile(df: pd.DataFrame, name: str) -> dict:
    return {"table": name, "rows": len(df), "columns": len(df.columns),
            "null_pct": {c: round(100 * df[c].isna().mean(), 1) for c in df.columns if df[c].isna().any()},
            "distinct": {c: int(df[c].nunique()) for c in df.columns[:12]}}


def run() -> dict:
    cfg = load_config()
    flags = Flags()
    o, a, w, ap = stage_opensky(cfg), stage_aviationstack(), stage_weather(cfg), stage_airports()
    log.info("staged rows: opensky=%d aviationstack=%d weather=%d airports=%d", len(o), len(a), len(w), len(ap))
    if o.empty:
        raise SourceError("validate: no OpenSky legs staged; nothing to measure")
    validate_opensky(o, cfg, flags)
    validate_aviationstack(a, flags)
    validate_weather(w, cfg, flags)
    validate_airports(ap, o, cfg, flags)
    fl = flags.frame()

    PROCESSED.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()  # rebuilt from raw on every run: rerun-safe, no state carried over
    with sqlite3.connect(DB_PATH) as con:
        o.to_sql("stg_opensky", con, index=False)
        a.to_sql("stg_aviationstack", con, index=False)
        w.to_sql("stg_weather", con, index=False)
        ap.to_sql("stg_airports", con, index=False)
        fl.to_sql("validation_flag", con, index=False)
    log.info("SQLite staged at %s; %d flag rows", DB_PATH.name, len(fl))

    crit = fl[fl.severity == "critical"]
    for rule, n in fl.groupby("rule").size().items():
        log.info("%s %-8s %6d  %s", rule, RULES[rule][0], n, RULES[rule][1])
    if not crit.empty:
        raise SourceError(f"validate: critical rule(s) failed: {sorted(crit.rule.unique())}")
    profiles = [profile(o, "stg_opensky"), profile(a, "stg_aviationstack"), profile(w, "stg_weather")]
    atomic_write(PROCESSED / "profile.json", json.dumps(profiles, indent=2))
    return {"opensky": len(o), "aviationstack": len(a), "weather": len(w), "airports": len(ap), "flags": len(fl)}


# ---------------------------------------------------------------- reporting (run after model.py adds V-DR flags)
def rule_table(con: sqlite3.Connection) -> pd.DataFrame:
    fl = pd.read_sql("SELECT entity, rule, COUNT(*) n FROM validation_flag GROUP BY entity, rule", con)
    base = {"opensky_leg": pd.read_sql("SELECT COUNT(*) n FROM stg_opensky WHERE is_crosscheck = 0", con).n[0],
            "aviationstack_row": pd.read_sql("SELECT COUNT(*) n FROM stg_aviationstack", con).n[0],
            "weather_hour": pd.read_sql("SELECT COUNT(*) n FROM stg_weather", con).n[0],
            "flight_leg": pd.read_sql("SELECT COUNT(*) n FROM flight_leg", con).n[0],
            "turnaround": pd.read_sql("SELECT COUNT(*) n FROM turnaround", con).n[0]}
    rows = []
    for rid, (sev, desc, why) in RULES.items():
        hit = fl[fl.rule == rid]
        n = int(hit.n.sum())
        ent = hit.entity.iloc[0] if len(hit) else None
        denom = base.get(ent)
        rows.append(dict(rule=rid, severity=sev, check=desc, why=why, flagged=n,
                         pct=f"{100 * n / denom:.1f}%" if denom and n else ("0" if not n else "")))
    return pd.DataFrame(rows)


def report() -> None:
    """Write output/validation_report.md and the AUTO block of the K/U/A/L doc from the final database."""
    with sqlite3.connect(DB_PATH) as con:
        rt = rule_table(con)
        stats = json.loads((PROCESSED / "model_stats.json").read_text()) if (PROCESSED / "model_stats.json").exists() else {}
    profiles = json.loads((PROCESSED / "profile.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = ["# Validation report\n", f"_Generated {stamp} by `src/validate.py` from the current raw snapshot._\n",
          "Rows are **flagged, never deleted**. `exclude` keeps the row in the database but out of KPI maths.\n",
          "## Rule results\n", "| Rule | Severity | Check | Flagged | % of rows | Why it matters |", "|---|---|---|---|---|---|"]
    md += [f"| {r.rule} | {r.severity} | {r.check} | {r.flagged:,} | {r.pct} | {r.why} |" for r in rt.itertuples()]
    md += ["\n## Profiles\n"]
    for p in profiles:
        md.append(f"**{p['table']}**: {p['rows']:,} rows × {p['columns']} columns. "
                  f"Null %: {p['null_pct'] or 'none'}. Distinct (first 12 cols): {p['distinct']}\n")
    atomic_write(OUTPUT / "validation_report.md", "\n".join(md) + "\n")

    s = stats
    auto = [f"### Measured on the current data (regenerated {stamp})\n",
            "| ID | Type | Finding (from this run) |", "|---|---|---|"]
    if s:
        auto += [
            f"| K-7 | Known | AviationStack's `+00:00` timestamps are **local IST**, not UTC: on the cross-check day, matched flights differ from OpenSky by a median **{s['tz_check']['median_diff_if_ist_min']:+.1f} min** if read as IST vs **{s['tz_check']['median_diff_if_utc_min']:+.1f} min** if read as UTC (n={s['tz_check']['n']}). |",
            f"| K-8 | Known | AviationStack `actual` = `actual_runway` in {s['as_actual_equals_runway_pct']:.0f}% of rows and sits {s['tz_check']['by_direction']['DEP']:+.1f} min from OpenSky wheels-up (departures) and {s['tz_check']['by_direction']['ARR']:+.1f} min from wheels-down (arrivals). It is itself ADS-B-derived runway time, **not an independent gate time**, so it cannot measure taxi-out (A-2 stays a config default, with sensitivity reported). |",
            f"| K-9 | Known | Indian carriers fly **alphanumeric callsigns** (e.g. `AIC7JN` operates `AIC2810`). Only {s['coverage']['direct_pct']:.0f}% of 5-group departures match a flight code directly; a callsign mapping learned from {s['xwalk_pairs']} cross-check matches lifts schedule coverage to **{s['coverage']['with_xwalk_pct']:.0f}%**. |",
            f"| L-6 | Limitation | {s['coverage']['unmatched_pct']:.0f}% of 5-group departures have no schedule match (flight code not in the Sept sample, or callsign changed since August). They count in traffic and turnarounds but not in delay KPIs (V-DR-1). |",
            f"| L-7 | Limitation | OpenSky loses {s['dep_dest_unknown_pct']:.0f}% of departures before they reach their destination (no `estArrivalAirport`). Domestic vs international falls back to the schedule's destination; {s['domestic_unknown_pct']:.0f}% of KPI departures stay unclassified. |",
            "| A-7 | Assumption (verified → K-7) | AviationStack times are local. Status: **verified** by cross-check. |",
            f"| K-10 | Known | {s['merged_round_trips']} OpenSky legs are BLR→BLR ~5 h round trips where OpenSky missed the outstation stop. Their take-offs are kept. Their landings are kept for turnarounds but excluded from arrival delay, because they carry the outbound callsign (V-OS-7). |",
            f"| K-11 | Known | The callsign mapping holds out of sample: OpenSky's observed destination is within 50 km of the scheduled one for {s['dest_agreement_pct_by_source'].get('callsign_map', float('nan')):.1f}% of mapped August legs vs {s['dest_agreement_pct_by_source'].get('flight_code', float('nan')):.1f}% of direct matches (A-4). |",
            f"| K-12 | Known | {s['retimed']['legs']} legs on {s['retimed']['flight_codes']} flight codes were retimed between August and the September sample (V-DR-5); {s['retimed']['legs_by_group'].get('Air India Group', 0)} of them Air India Group. Left in, they would have inflated that group's delays. |",
        ]
    auto += ["", "Rule-by-rule counts: see `output/validation_report.md`."]
    kua = DOCS / "known_unknown_assumptions.md"
    text = kua.read_text()
    new = re.sub(r"<!-- AUTO:START.*?<!-- AUTO:END -->",
                 "<!-- AUTO:START (generated by src/validate.py, do not edit by hand) -->\n" + "\n".join(auto) + "\n<!-- AUTO:END -->",
                 text, flags=re.S)
    atomic_write(kua, new)
    log.info("wrote output/validation_report.md and AUTO block of docs/known_unknown_assumptions.md")


if __name__ == "__main__":
    print(run())
