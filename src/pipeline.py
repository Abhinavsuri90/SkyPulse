"""SkyPulse pipeline - the single entrypoint: ingest -> validate -> model -> metrics -> benchmark -> report.

    python src/pipeline.py                  # normal run: reuse raw snapshot, fetch anything missing
    python src/pipeline.py --offline        # prove reproducibility: no network at all, raw snapshot only
    python src/pipeline.py --refresh weather,dgca
    python src/pipeline.py --fault weather  # failure drill: weather API unreachable -> must fail loudly

Dependability:
  * rerun-safe: raw inputs are reused, the SQLite model is rebuilt from scratch, outputs are overwritten atomically
  * every external call retries with backoff, then fails loudly; nothing degrades to an empty metric
  * a failed run writes output/LAST_RUN_FAILED.md + output/run_status.json and exits non-zero;
    the last good raw files are never overwritten by a failed fetch
  * row counts are logged at every stage (and reconciled in model.py)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import benchmark
import ingest
import metrics
import model
import validate
from common import OUTPUT, atomic_write, get_logger

log = get_logger("pipeline")
FAIL_MARKER = OUTPUT / "LAST_RUN_FAILED.md"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", default="", help="comma list of sources to re-download (ourairports,weather,dgca,opensky) or 'all'")
    ap.add_argument("--offline", action="store_true", help="forbid all network calls; run from the committed raw snapshot")
    ap.add_argument("--fault", default="", help="failure drill: make this source's endpoint unreachable (implies --refresh of it)")
    ap.add_argument("--aviationstack-calls", type=int, default=0, help="spend up to N AviationStack calls (ledger-capped)")
    a = ap.parse_args()

    refresh = {s for s in a.refresh.split(",") if s}
    if a.offline:
        os.environ["SKYPULSE_OFFLINE"] = "1"
    if a.fault:
        os.environ["SKYPULSE_FAULT"] = a.fault
        refresh.add(a.fault)

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
    for name, fn in stages:
        t0 = time.time()
        log.info("---- stage %s", name)
        try:
            result = fn()
        except Exception as e:  # any stage failure stops the run; nothing downstream runs on bad input
            status = {"run_id": run_id, "status": "FAILED", "failed_stage": name, "error": f"{type(e).__name__}: {e}",
                      "completed_stages": done, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            atomic_write(OUTPUT / "run_status.json", json.dumps(status, indent=2))
            atomic_write(FAIL_MARKER, f"# Last pipeline run FAILED\n\nRun `{run_id}` failed at stage **{name}**:\n\n"
                                      f"```\n{status['error']}\n```\n\nOutputs in this folder are from the last *successful* run. "
                                      f"Nothing was overwritten by the failed run.\n")
            log.error("==== RUN FAILED at stage '%s': %s", name, status["error"])
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
