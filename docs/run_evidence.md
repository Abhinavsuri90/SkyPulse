# Pipeline dependability evidence

Three runs, made on 2026-09-24 on the final pipeline, cover the three things the rubric asks for: failure handling, rerun behaviour and logging. Excerpts are copied from the log output (full logs, including tracebacks, go to `logs/`, which is git-ignored).

## 1. Deliberate failure: the weather API is unreachable

```
python src/pipeline.py --fault weather
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260924T000751Z start (offline=False, refresh=['weather'], fault=weather)
WARNING | skypulse.ingest | FAULT INJECTION active for weather: using an unreachable endpoint
WARNING | skypulse.ingest | open-meteo: attempt 1/3 failed (ConnectionError: fault-injection.invalid unreachable ...)
WARNING | skypulse.ingest | open-meteo: attempt 2/3 failed (ConnectionError: fault-injection.invalid unreachable ...)
ERROR   | skypulse.pipeline | ==== RUN FAILED at stage 'ingest': SourceError: open-meteo: failed after 3 attempts (ConnectionError: fault-injection.invalid unreachable ...)
```

What this shows:
- **Retries with backoff**: 3 attempts, waiting 2 s then 4 s.
- **Fails loudly**: exit code 1. `output/LAST_RUN_FAILED.md` and `output/run_status.json` name the stage and the error.
- **No silent damage**: the last good weather file was not replaced. Its sha256 was `7b9f5823c22cded8…` before and after. `output/evidence_table.md` was not touched either (same modification time before and after). No metric was computed on missing weather.
- **Secrets stay out of errors**: request errors are scrubbed of API keys before logging (test `test_secrets_never_reach_error_messages`).

## 2. Recovery run (normal, online)

```
python src/pipeline.py
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260924T000801Z start (offline=False, refresh=[], fault=None)
INFO    | skypulse.pipeline | ---- stage ingest OK in 1.6s: {'ourairports': 86119, 'open_meteo': 744, 'dgca': 18, 'opensky': 20549, 'aviationstack': 1486}
INFO    | skypulse.pipeline | ---- stage validate OK in 1.1s: {'opensky': 21183, 'aviationstack': 1486, 'weather': 744, 'airports': 86119, 'flags': 15931}
INFO    | skypulse.pipeline | ---- stage model OK in 2.7s: {'flight_leg_rows': 20549, 'in_kpi': {'DEP': 6430, 'ARR': 5966}, 'turnarounds': {'pairs': 9675, 'valid': 9276}}
INFO    | skypulse.pipeline | ---- stage metrics OK in 3.9s: {'otp_pct': 91.6, 'n': 6430}
INFO    | skypulse.pipeline | ---- stage benchmark OK in 4.5s: {'ours_pct': 91.8, 'dgca_pct': 92.0, 'gap_pts': -0.2}
INFO    | skypulse.pipeline | ---- stage report OK in 0.1s: None
INFO    | skypulse.pipeline | ==== SkyPulse run 20260924T000801Z OK. Outputs: output/evidence_table.md, output/benchmark_comparison.md
```

`LAST_RUN_FAILED.md` is removed once a run succeeds.

## 3. Rerun from the committed raw snapshot, with no network

```
python src/pipeline.py --offline
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260924T000817Z start (offline=True, refresh=[], fault=None)
INFO    | skypulse.pipeline | ---- stage ingest OK in 1.4s: {'ourairports': 86119, 'open_meteo': 744, 'dgca': 18, 'opensky': 20549, 'aviationstack': 1486}
INFO    | skypulse.pipeline | ---- stage validate OK in 0.7s: {'opensky': 21183, 'aviationstack': 1486, 'weather': 744, 'airports': 86119, 'flags': 15931}
INFO    | skypulse.pipeline | ---- stage model OK in 1.3s: {'flight_leg_rows': 20549, 'in_kpi': {'DEP': 6430, 'ARR': 5966}, 'turnarounds': {'pairs': 9675, 'valid': 9276}}
INFO    | skypulse.pipeline | ---- stage metrics OK in 2.0s: {'otp_pct': 91.6, 'n': 6430}
INFO    | skypulse.pipeline | ---- stage benchmark OK in 1.9s: {'ours_pct': 91.8, 'dgca_pct': 92.0, 'gap_pts': -0.2}
INFO    | skypulse.pipeline | ---- stage report OK in 0.0s: None
INFO    | skypulse.pipeline | ==== SkyPulse run 20260924T000817Z OK. Outputs: output/evidence_table.md, output/benchmark_comparison.md
```

**Rerun-safe and deterministic.** Runs 2 and 3 produced byte-identical `data/processed/metrics.json` (sha256 of the content excluding its timestamp: `2971f01c7fe9cf43…` both times). The SQLite model is rebuilt from scratch every run, so no state carries over between runs. CI repeats this check on every push.

## Row-count trail (a silent data loss would break one of these)

| Stage | Check |
|---|---|
| ingest | 62 OpenSky day-files, per-day count vs floor and vs 70 % of the window median. 744 / 744 weather hours. AviationStack `pagination.total` vs rows fetched, per airline and direction. |
| validate | Every staged row is kept; rule hits go to `validation_flag` (15,931 flags on 21,183 legs + 1,486 schedule rows) |
| model | **Reconciliation assert:** 20,549 raw window legs = 20,549 `flight_leg` rows, or the run fails |
| metrics | OTP computed twice, in SQL and in pandas; the run fails if they differ by more than 0.1 pt |
| benchmark | DGCA parse checks V-DG-1…5. A critical failure (e.g. BLR value missing) stops the run. |
