# Pipeline dependability evidence

Four runs, made one after another on 2026-09-26 on the final pipeline, cover the three things the rubric asks for: failure handling, rerun behaviour and logging. Excerpts are copied from the log output (full logs, including tracebacks, go to `logs/`, which is git-ignored). Long error messages are shortened with `...`.

**The guarantee:** before any stage runs, the pipeline copies every output (`output/`, `data/processed/`, and the generated block of the K/U/A/L register). If any stage fails, all of it is put back, so outputs never mix two runs. CI repeats drill 2 on every push.

## 1. Deliberate failure at the start: the weather API is unreachable

```
python src/pipeline.py --fault weather
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130255Z start (offline=False, refresh=['weather'], fault=weather)
WARNING | skypulse.ingest | FAULT INJECTION active for weather: using an unreachable endpoint
WARNING | skypulse.ingest | open-meteo: attempt 1/3 failed (ConnectionError: ... Failed to resolve 'fault-injection.invalid' ...); retrying in 2s
WARNING | skypulse.ingest | open-meteo: attempt 2/3 failed (ConnectionError: ... Failed to resolve 'fault-injection.invalid' ...); retrying in 4s
ERROR   | skypulse.pipeline | ==== RUN FAILED at stage 'ingest': SourceError: open-meteo: failed after 3 attempts (ConnectionError: ...)
ERROR   | skypulse.pipeline | outputs restored to the last successful run (0 completed stage(s) rolled back)
```

What this shows:
- **Retries with backoff**: 3 attempts, waiting 2 s then 4 s.
- **Fails loudly**: exit code 1. `output/LAST_RUN_FAILED.md` and `output/run_status.json` name the stage and the error.
- **No silent damage**: the last good weather file was not replaced. Its sha256 was `7b9f5823c22cded8…` before and after. The hash of every output file was `251f685db0dcd27b…` before and after. No metric was computed on missing weather.
- **Secrets stay out of errors**: request errors are scrubbed of API keys before logging (test `test_secrets_never_reach_error_messages`).

## 2. Deliberate failure late in the run: earlier outputs are rolled back

A failure in ingest is the easy case, because nothing has been written yet. A late failure is harder: by the time the benchmark stage runs, the metrics stage has already rewritten `evidence_table.md` and `metrics.json`.

```
python src/pipeline.py --offline --fault benchmark
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130404Z start (offline=True, refresh=['benchmark'], fault=benchmark)
INFO    | skypulse.pipeline | ---- stage ingest OK in 0.7s: {'ourairports': 86119, 'open_meteo': 744, 'dgca': 18, 'opensky': 20549, 'aviationstack': 1486}
INFO    | skypulse.pipeline | ---- stage validate OK in 0.6s: {'opensky': 21183, 'aviationstack': 1486, 'weather': 744, 'airports': 86119, 'flags': 15931}
INFO    | skypulse.pipeline | ---- stage model OK in 1.2s: {'flight_leg_rows': 20549, 'in_kpi': {'DEP': 6430, 'ARR': 5966}, 'turnarounds': {'pairs': 9675, 'valid': 9276}}
INFO    | skypulse.pipeline | ---- stage metrics OK in 1.9s: {'otp_pct': 91.6, 'n': 6430}
WARNING | skypulse.pipeline | FAULT INJECTION active: stage benchmark forced to fail
ERROR   | skypulse.pipeline | ==== RUN FAILED at stage 'benchmark': RuntimeError: fault injection: stage 'benchmark' forced to fail
ERROR   | skypulse.pipeline | outputs restored to the last successful run (4 completed stage(s) rolled back)
```

What this shows:
- `output/evidence_table.md` still says _Generated 2026-09-26 13:01 UTC_ (the last good run), although the failed run's metrics stage had rewritten it a minute later.
- The hash of every output file (excluding the two status files) was `251f685db0dcd27b…` before and after.
- The same drill runs in CI on every push, and `test_failed_late_stage_restores_every_output` covers it in the test suite.

## 3. Recovery run (normal, online)

```
python src/pipeline.py
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130409Z start (offline=False, refresh=[], fault=None)
INFO    | skypulse.pipeline | ---- stage ingest OK in 0.7s: {'ourairports': 86119, 'open_meteo': 744, 'dgca': 18, 'opensky': 20549, 'aviationstack': 1486}
INFO    | skypulse.pipeline | ---- stage validate OK in 0.6s: {'opensky': 21183, 'aviationstack': 1486, 'weather': 744, 'airports': 86119, 'flags': 15931}
INFO    | skypulse.pipeline | ---- stage model OK in 1.3s: {'flight_leg_rows': 20549, 'in_kpi': {'DEP': 6430, 'ARR': 5966}, 'turnarounds': {'pairs': 9675, 'valid': 9276}}
INFO    | skypulse.pipeline | ---- stage metrics OK in 1.8s: {'otp_pct': 91.6, 'n': 6430}
INFO    | skypulse.pipeline | ---- stage benchmark OK in 1.8s: {'ours_pct': 91.8, 'dgca_pct': 92.0, 'gap_pts': -0.2}
INFO    | skypulse.pipeline | ---- stage report OK in 0.0s: None
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130409Z OK. Outputs: output/evidence_table.md, output/benchmark_comparison.md
```

`LAST_RUN_FAILED.md` is removed once a run succeeds.

## 4. Rerun from the committed raw snapshot, with no network

```
python src/pipeline.py --offline
```

```
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130416Z start (offline=True, refresh=[], fault=None)
INFO    | skypulse.pipeline | ---- stage ingest OK in 0.6s: {'ourairports': 86119, 'open_meteo': 744, 'dgca': 18, 'opensky': 20549, 'aviationstack': 1486}
INFO    | skypulse.pipeline | ---- stage validate OK in 0.5s: {'opensky': 21183, 'aviationstack': 1486, 'weather': 744, 'airports': 86119, 'flags': 15931}
INFO    | skypulse.pipeline | ---- stage model OK in 1.2s: {'flight_leg_rows': 20549, 'in_kpi': {'DEP': 6430, 'ARR': 5966}, 'turnarounds': {'pairs': 9675, 'valid': 9276}}
INFO    | skypulse.pipeline | ---- stage metrics OK in 1.8s: {'otp_pct': 91.6, 'n': 6430}
INFO    | skypulse.pipeline | ---- stage benchmark OK in 1.8s: {'ours_pct': 91.8, 'dgca_pct': 92.0, 'gap_pts': -0.2}
INFO    | skypulse.pipeline | ---- stage report OK in 0.0s: None
INFO    | skypulse.pipeline | ==== SkyPulse run 20260926T130416Z OK. Outputs: output/evidence_table.md, output/benchmark_comparison.md
```

**Rerun-safe and deterministic.** Runs 3 and 4 produced byte-identical `data/processed/metrics.json` (sha256 of the content excluding its timestamp: `d51609f6dda52b4b…` both times). The SQLite model is rebuilt from scratch every run, so no state carries over between runs. CI repeats this check on every push.

## Row-count trail (a silent data loss would break one of these)

| Stage | Check |
|---|---|
| ingest | 62 OpenSky day-files, per-day count vs floor and vs 70 % of the window median. 744 / 744 weather hours. AviationStack `pagination.total` vs rows fetched, per airline and direction. |
| validate | Every staged row is kept; rule hits go to `validation_flag` (15,931 flags on 21,183 legs + 1,486 schedule rows) |
| model | **Reconciliation assert:** 20,549 raw window legs = 20,549 `flight_leg` rows, or the run fails |
| metrics | OTP computed twice, in SQL and in pandas; the run fails if they differ by more than 0.1 pt |
| benchmark | DGCA parse checks V-DG-1…5. A critical failure (e.g. BLR value missing) stops the run. |
