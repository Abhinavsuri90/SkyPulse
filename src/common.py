"""Shared plumbing: config, paths, logging, HTTP with retry, raw-file manifest, time window."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
DOCS = ROOT / "docs"
MANIFEST = RAW / "_manifest.json"
DB_PATH = PROCESSED / "skypulse.sqlite"

SECRET_PARAMS = {"access_key", "client_secret", "client_id"}


class SourceError(RuntimeError):
    """An external source failed after retries, or returned something unusable. Stops the pipeline."""


class CredentialMissing(RuntimeError):
    """A source needs a credential that is not in .env. Raised loudly, never skipped silently."""


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def require_env(*names: str) -> dict[str, str]:
    load_dotenv(ROOT / ".env")
    values = {n: os.environ.get(n, "").strip() for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise CredentialMissing(
            f"Missing credential(s): {', '.join(missing)}. Add them to {ROOT / '.env'} "
            f"(see .env.example). This source is blocked until they are provided."
        )
    return values


# ---------------------------------------------------------------- logging
def get_logger(name: str) -> logging.Logger:
    """Console (INFO) + one log file per process run under logs/ (DEBUG, incl. full tracebacks)."""
    root = logging.getLogger("skypulse")
    if not root.handlers:
        LOGS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        script = Path(sys.argv[0]).stem or "interactive"
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-9s | %(message)s", "%Y-%m-%d %H:%M:%S")
        root.setLevel(logging.DEBUG)
        fh = logging.FileHandler(LOGS / f"{script}_{stamp}.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        root.addHandler(fh)
        root.addHandler(sh)
    return logging.getLogger(f"skypulse.{name}")


# ---------------------------------------------------------------- HTTP
def redact(params: dict | None) -> dict:
    return {k: ("***" if k in SECRET_PARAMS else v) for k, v in (params or {}).items()}


def scrub(text: str, *payloads: dict | None) -> str:
    """Remove secret values from an error message (requests puts the full URL, query string included, in its errors)."""
    for p in payloads:
        for k, v in (p or {}).items():
            if k in SECRET_PARAMS and v:
                text = text.replace(str(v), "***")
    return text


def http_request(
    method: str,
    url: str,
    *,
    log: logging.Logger,
    label: str,
    params: dict | None = None,
    data: dict | None = None,
    headers: dict | None = None,
    empty_statuses: tuple[int, ...] = (),
    cfg: dict | None = None,
) -> requests.Response | None:
    """Request with retry + exponential backoff.

    Retries network errors, 429 and 5xx. Other 4xx fail immediately (retrying a bad request
    or a bad credential never helps). Statuses in `empty_statuses` mean "legitimately no data"
    and return None. After the last retry the call raises SourceError; it never returns
    a silent empty result.
    """
    if os.environ.get("SKYPULSE_OFFLINE") == "1":
        raise SourceError(f"{label}: offline mode (--offline) and this raw input is not in data/raw yet")
    http = (cfg or load_config())["http"]
    retries, backoff, timeout = http["retries"], http["backoff_s"], http["timeout_s"]
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
            if r.status_code in empty_statuses:
                log.info("%s: HTTP %s -> treated as 'no data for this interval'", label, r.status_code)
                return None
            if r.status_code == 200:
                return r
            last_err = scrub(f"HTTP {r.status_code}: {r.text[:200]}", params, data)
            if r.status_code != 429 and r.status_code < 500:
                raise SourceError(f"{label}: non-retryable {last_err}")
        except requests.RequestException as e:
            last_err = scrub(f"{type(e).__name__}: {e}", params, data)
        if attempt < retries:
            wait = backoff * 2 ** (attempt - 1)
            log.warning("%s: attempt %d/%d failed (%s); retrying in %ss", label, attempt, retries, last_err, wait)
            time.sleep(wait)
    raise SourceError(f"{label}: failed after {retries} attempts ({last_err})")


# ---------------------------------------------------------------- raw files + manifest
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, payload: bytes | str) -> None:
    """Write to a temp file then rename, so a failed run never leaves a half-written or deleted raw file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload.encode() if isinstance(payload, str) else payload)
    tmp.replace(path)


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def record_raw(path: Path, *, source: str, url: str, params: dict | None, rows: int | None,
               status: str = "ok", note: str = "") -> None:
    """One manifest entry per raw file: provenance + row count + hash = evidence that retrieval is complete."""
    manifest = load_manifest()
    rel = str(path.relative_to(ROOT))
    manifest[rel] = {
        "source": source,
        "url": url,
        "params": redact(params),
        "retrieved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "status": status,
        "note": note,
    }
    atomic_write(MANIFEST, json.dumps(dict(sorted(manifest.items())), indent=2))


# ---------------------------------------------------------------- time window
def tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["airport"]["timezone"])


def local_days(cfg: dict) -> list[date]:
    w = cfg["analysis_window"]
    d0, d1 = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def day_bounds_epoch(day: date, cfg: dict) -> tuple[int, int]:
    """[local midnight, next local midnight) as UTC epoch seconds."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz(cfg))
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())
