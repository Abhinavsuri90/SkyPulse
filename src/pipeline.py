"""SkyPulse pipeline - the single entrypoint: ingest -> validate -> model -> metrics -> benchmark -> report.

    python src/pipeline.py                  # normal run: reuse raw snapshot, fetch anything missing
    python src/pipeline.py --offline        # prove reproducibility: no network at all, raw snapshot only
    python src/pipeline.py --refresh weather,dgca
    python src/pipeline.py --fault weather  # failure drill: weather API unreachable -> must fail loudly
    python src/pipeline.py --offline --fault benchmark  # failure drill: a late stage fails after earlier ones wrote outputs

Dependability:
  * rerun-safe: raw inputs are reused, the SQLite model is rebuilt from scratch, outputs are overwritten atomically
  * every external call retries with backoff, then fails loudly; nothing degrades to an empty metric
  * a failed run puts every output back exactly as the last good run left it, then writes
    output/LAST_RUN_FAILED.md + output/run_status.json and exits non-zero;
    the last good raw files are never overwritten by a failed fetch
  * row counts are logged at every stage (and reconciled in model.py)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import benchmark
import ingest
import metrics
import model
import validate
from common import DOCS, OUTPUT, PROCESSED, atomic_write, get_logger

log = get_logger("pipeline")
FAIL_MARKER = OUTPUT / "LAST_RUN_FAILED.md"
# Everything a stage may rewrite. A failed run restores all of it, so outputs never mix two runs.
GUARDED = [OUTPUT, PROCESSED, DOCS / "known_unknown_assumptions.md"]


def snapshot(paths: list[Path], into: Path) -> None:
    for i, p in enumerate(paths):
        if p.is_dir():
            shutil.copytree(p, into / str(i))
        elif p.exists():
            shutil.copy2(p, into / str(i))


def restore(paths: list[Path], snap: Path) -> None:
    """Put each guarded path back as it was, removing anything the failed run created."""
    for i, p in enumerate(paths):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        src = snap / str(i)
        if src.is_dir():
            shutil.copytree(src, p)
        elif src.exists():
            shutil.copy2(src, p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", default="", help="comma list of sources to re-download (ourairports,weather,dgca,opensky) or 'all'")
    ap.add_argument("--offline", action="store_true", help="forbid all network calls; run from the committed raw snapshot")
    ap.add_argument("--fault", default="", help="failure drill: comma list of sources (endpoint made unreachable, implies "
                    "--refresh) and/or stages (forced to fail)")
    ap.add_argument("--aviationstack-calls", type=int, default=0, help="spend up to N AviationStack calls (ledger-capped)")
    a = ap.parse_args()

    refresh = {s for s in a.refresh.split(",") if s}
    faults = {s for s in a.fault.split(",") if s}
    if a.offline:
        os.environ["SKYPULSE_OFFLINE"] = "1"
    if faults:
        os.environ["SKYPULSE_FAULT"] = a.fault
        refresh |= faults  # stage names in here are ignored by ingest

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stages = [
        ("ingest", lambda: ingest.run(refresh=refresh, aviationstack_calls=a.aviationstack_calls)),
        ("validate", validate.run),
        ("model", model.run),
        ("metrics", metrics.run),
        ("benchmark", benchmark.run),
        ("report", validate.report),
    ]
    log.info("==== SkyPulse run %s start (offline=%s, refresh=%s, fault=%s)", run_id, a.offline, sorted(refresh), a.fault or None)
    done = []
    with tempfile.TemporaryDirectory(prefix="skypulse-last-good-") as tmp:
        snapshot(GUARDED, Path(tmp))
        for name, fn in stages:
            t0 = time.time()
            log.info("---- stage %s", name)
            try:
                if name in faults:
                    log.warning("FAULT INJECTION active: stage %s forced to fail", name)
                    raise RuntimeError(f"fault injection: stage '{name}' forced to fail")
                result = fn()
            except Exception as e:  # any stage failure stops the run; nothing downstream runs on bad input
                restore(GUARDED, Path(tmp))
                status = {"run_id": run_id, "status": "FAILED", "failed_stage": name, "error": f"{type(e).__name__}: {e}",
                          "completed_stages": done, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                atomic_write(OUTPUT / "run_status.json", json.dumps(status, indent=2, default=str))
                atomic_write(FAIL_MARKER, f"# Last pipeline run FAILED\n\nRun `{run_id}` failed at stage **{name}**:\n\n"
                                          f"```\n{status['error']}\n```\n\nEvery other file in `output/` and `data/processed/` "
                                          f"was restored to the last *successful* run.\n")
                log.error("==== RUN FAILED at stage '%s': %s", name, status["error"])
                log.error("outputs restored to the last successful run (%d completed stage(s) rolled back)", len(done))
                log.debug("traceback:\n%s", traceback.format_exc())  # full trace goes to the log file only
                return 1
            secs = round(time.time() - t0, 1)
            done.append({"stage": name, "seconds": secs, "result": summarize(result)})
            log.info("---- stage %s OK in %.1fs: %s", name, secs, summarize(result))

    FAIL_MARKER.unlink(missing_ok=True)
    atomic_write(OUTPUT / "run_status.json", json.dumps(
        {"run_id": run_id, "status": "OK", "stages": done, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        indent=2, default=str))
    log.info("==== SkyPulse run %s OK. Outputs: output/evidence_table.md, output/benchmark_comparison.md", run_id)
    return 0


def summarize(result) -> object:
    """Row counts / headline numbers only, for the log and run_status.json."""
    if isinstance(result, list):  # ingest summaries
        return {r["source"]: r["rows"] for r in result}
    if isinstance(result, dict):
        if "m1_otp" in result:
            return {"otp_pct": result["m1_otp"]["value_pct"], "n": result["m1_otp"]["n"]}
        if "ours_pct" in result:
            return {"ours_pct": result["ours_pct"], "dgca_pct": result["dgca_blr_pct"], "gap_pts": result["gap_pts"]}
        if "flight_leg_rows" in result:
            return {"flight_leg_rows": result["flight_leg_rows"], "in_kpi": result["legs_in_kpi"], "turnarounds": result["turnarounds"]}
        return result
    return result


if __name__ == "__main__":
    sys.exit(main())
