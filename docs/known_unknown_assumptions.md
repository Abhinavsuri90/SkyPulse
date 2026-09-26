# Known / Unknown / Assumption / Limitation

The rule this project follows: **no row is silently patched, dropped or interpolated.** Every rule that excludes data, and every leap the numbers depend on, is written here with an ID. Code and outputs cite these IDs.

- The hand-written entries below were recorded as each decision was made.
- The block between the `AUTO` markers is rewritten by `src/validate.py` on every pipeline run, so its numbers always match the current data.

---

## Known (verified facts)

| ID | Fact | Evidence |
|---|---|---|
| K-1 | DGCA publishes **BLR On-Time Performance = 92.0 %** for August 2026. It covers five domestic airline groups (IndiGo, Air India Group, Akasa Air, SpiceJet, Alliance Air). | `data/raw/dgca/dgca_traffic_report_2026-08.pdf`, p. 6 |
| K-2 | DGCA's OTP rule is the same as our KPI: "a delay of more than 15 minutes from the scheduled departure time is considered a delayed flight." | same PDF, p. 9 note |
| K-3 | OurAirports `airports.csv` has **no timezone column** (the project brief assumed one). India is a single zone (IST, UTC+05:30, no DST), so the zone is fixed in `config.yaml`. | header row of `data/raw/ourairports/airports.csv` |
| K-4 | OpenSky refuses anonymous access to historical flights (`HTTP 403 "You cannot access historical flights"`, tested 2026-09-23), so a registered OAuth2 client is required. | ingest probe |
| K-5 | **The official DGCA report itself has a defect.** Its Alliance Air airport chart lists `GAU` twice with different values (92.0 and 63.5). We use no Alliance Air GAU value; BLR is unaffected. | DGCA PDF p. 7 |
| K-6 | DGCA's national delay-cause split for August 2026: Reactionary 65 %, Ops 8 %, Tech 6 %, ATC 6 %, Weather 5 %, Airport 3 %, Misc 3 %, Pax 2 %, Ramp 2 %. | DGCA PDF p. 9 |

## Unknown (cannot be known from available sources)

| ID | Unknown | Why it matters | How we handle it |
|---|---|---|---|
| U-1 | True **gate** (off-block / in-block) times for August flights | They are the official basis for OTP | Estimated from runway times (A-1, A-2). This is the largest source of gap versus DGCA. |
| U-2 | **Cancelled** flights | OpenSky only sees aircraft that flew, so cancellations never appear | OTP here is "of flights that operated", as with DGCA, which reports cancellations separately (1.39 % national, Aug 2026) |
| U-3 | Whether each flight code kept **exactly** the same scheduled time for all of August | Our schedule reference comes from a later sample (A-3) | Two rules catch mismatches and exclude them, never clip: V-DR-2 (\|delay\| outside −60…+720 min) and **V-DR-5 (retimes)**, a flight offset from its schedule on ≥ 80 % of its operations, either by a large amount (> 60 min) or by a steady amount while the September sample shows the same flight on time. A real delay varies day to day; a changed schedule doesn't. The benchmark's close agreement depends on this rule (88.3 % with retimes left in, vs 91.8 %), so its effect and the evidence for every code are published in `output/benchmark_comparison.md`. |
| U-4 | The **delay reason** for each flight | DGCA publishes only a national split | We infer a *delay attribution* (inbound-late / weather-exposed / ground-side) from the data. It is labelled as inference. |

## Assumptions (leaps the numbers depend on)

| ID | Assumption | Why it's reasonable | Sensitivity / check |
|---|---|---|---|
| A-1 | OpenSky `firstSeen` on a VOBL departure ≈ **wheels-up**; `lastSeen` on a VOBL arrival ≈ **wheels-down** | Median first detection is 3.9 km / 330 m from BLR, i.e. about a minute after take-off. Legs detected far away are flagged (V-OS-6). | AviationStack's runway times sit a median +1.4 min (departures) and −1.6 min (arrivals) from OpenSky's on the cross-check day (K-8) |
| A-2 | **Gate time = runway time ∓ taxi allowance**: 15 min taxi-out, 8 min taxi-in (`config.yaml`) | Schedules and DGCA use gate times, OpenSky gives runway times, and **no available source measures BLR taxi times**. The plan was to measure them from AviationStack, but its `actual` is runway time too (K-8). | OTP is reported for 0–25 min allowances. The decision holds for allowances of 10–20 min: the airline ranking is stable from 10 min, and ground-side leads up to 20 min, with reactionary overtaking at 25. DGCA's published OTP would be matched at 16 min, inside that window; that is shown as a diagnostic and never used to tune the allowance. |
| A-3 | A flight code's scheduled local time in the September AviationStack sample **equals its August schedule** | Same IATA summer season (S26 runs to late October). In the sample, 478 of 493 flight codes seen on two days kept the same time (97 %). | Retimes are detected and excluded (V-DR-5, U-3); the share of legs matched is reported |
| A-4 | An OpenSky `callsign` identifies its flight either **directly** (`IGO2145` = flight `IGO2145`) or through a **callsign → flight mapping learned on the cross-check day** by matching the same aircraft (`icao24`) at the same time (`AIC7JN` → `AIC2810`). The mapping is assumed to hold for August. | Indian carriers use alphanumeric ATC callsigns, assigned per flight for the season (K-9) | Tested out of sample on August: OpenSky's observed destination agrees with the schedule's as often for mapped legs as for direct matches (see AUTO block). Ambiguous mappings are dropped (V-DR-3). |
| A-5 | An hour is **adverse weather** if precipitation ≥ 1 mm, gusts ≥ **55** km/h, visibility < 3 km, or there is a thunderstorm (WMO 95/96/99) | **Revised from 40 km/h gusts.** In August BLR's monsoon gusts run p50 = 33 and p90 = 47 km/h, so 40 km/h flagged 265 of 744 hours (36 %). That describes the climate, not adverse weather. 55 km/h (~30 kt) leaves 38 adverse hours. The change was made **before** delays were examined, so it isn't tuned to the result. | Thresholds live in `config.yaml` |
| A-6 | **Turnaround** = next take-off of the same `icao24` after it lands at BLR, kept only when it falls in 20 min – 12 h | Shorter is a data artefact; longer is an aircraft parked overnight, not an operational turn | Excluded pairs are counted and reported (V-DR-4) |
| A-8 | A departure is **reactionary** if its inbound aircraft reached the gate so late that a **30-min** minimum turn could not make STD + 15 | Planning minimum for narrow-body domestic turns; stricter than airline-coded 'reactionary' | Our reactionary share (28 %) is below DGCA's national 65 %. This is discussed openly in the benchmark as the main challenge to the recommendation. |

## Limitations (known weaknesses of the result)

| ID | Limitation | Effect on conclusions |
|---|---|---|
| L-1 | OpenSky is crowd-sourced. Some flights are missed or have an estimated airport that's wrong. | Counts slightly under-state true traffic. Per-day completeness is checked at ingest (0 of 62 day-files flagged as partial). |
| L-2 | AviationStack free tier: ≤ 30 calls used (100/month cap), real-time only | The schedule reference covers the flight codes seen in the sample, not every August flight |
| L-3 | The DGCA benchmark is **airline self-reported**, covers **domestic** flights of **5 airline groups** only, and gives no uncertainty | Benchmark comparison is restricted to the same population; residual gap is explained, not forced to zero |
| L-4 | Open-Meteo is **modelled** weather, not observed METAR | Short convective storms may be mistimed by an hour; mitigated by a 1-hour lookback |
| L-5 | One month (August 2026, monsoon). | Findings describe monsoon operations; winter fog season (Dec–Jan) would need a rerun (change two dates in `config.yaml`) |
| L-8 | OpenSky sometimes misses an aircraft's inbound landing at BLR, so for some delayed departures we can't tell whether the aircraft was there in time | Those delays get their own bucket, **inbound not observed** (27 of 539 in August), rather than defaulting to ground-side. Ground-side requires positive evidence: an inbound landing seen in time, or the aircraft parked at BLR for more than 12 h. |

---

<!-- AUTO:START (generated by src/validate.py, do not edit by hand) -->
### Measured on the current data (regenerated 2026-09-26 14:20 UTC)

| ID | Type | Finding (from this run) |
|---|---|---|
| K-7 | Known | AviationStack's `+00:00` timestamps are **local IST**, not UTC: on the cross-check day, matched flights differ from OpenSky by a median **+0.9 min** if read as IST vs **-329.1 min** if read as UTC (n=483). |
| K-8 | Known | AviationStack `actual` = `actual_runway` in 100% of rows and sits +1.4 min from OpenSky wheels-up (departures) and -1.6 min from wheels-down (arrivals). It is itself ADS-B-derived runway time, **not an independent gate time**, so it cannot measure taxi-out (A-2 stays a config default, with sensitivity reported). |
| K-9 | Known | Indian carriers fly **alphanumeric callsigns** (e.g. `AIC7JN` operates `AIC2810`). Only 44% of 5-group departures match a flight code directly; a callsign mapping learned from 489 cross-check matches lifts schedule coverage to **77%**. |
| L-6 | Limitation | 23% of 5-group departures have no schedule match (flight code not in the Sept sample, or callsign changed since August). They count in traffic and turnarounds but not in delay KPIs (V-DR-1). |
| L-7 | Limitation | OpenSky loses 43% of departures before they reach their destination (no `estArrivalAirport`). Domestic vs international falls back to the schedule's destination; 0% of KPI departures stay unclassified. |
| A-7 | Assumption (verified → K-7) | AviationStack times are local. Status: **verified** by cross-check. |
| K-10 | Known | 523 OpenSky legs are BLR→BLR ~5 h round trips where OpenSky missed the outstation stop. Their take-offs are kept. Their landings are kept for turnarounds but excluded from arrival delay, because they carry the outbound callsign (V-OS-7). |
| K-11 | Known | The callsign mapping holds out of sample: OpenSky's observed destination is within 50 km of the scheduled one for 94.7% of mapped August legs vs 94.2% of direct matches (A-4). |
| K-12 | Known | 429 legs on 20 flight codes were retimed between August and the September sample (V-DR-5); 365 of them Air India Group. Left in, they would have inflated that group's delays. |

Rule-by-rule counts: see `output/validation_report.md`.
<!-- AUTO:END -->
