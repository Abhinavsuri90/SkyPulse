# SkyPulse evidence table: BLR, 2026-08-01 → 2026-08-31

_Generated 2026-09-24 00:08 UTC by `src/metrics.py` from the committed raw snapshot. Every number is reproducible with `python src/pipeline.py`._

**KPI:** On-Time Departure Rate. **Population:** BLR departures that have a real schedule match (AviationStack) and pass every validation rule. That is 6,430 of 10,157 observed departures; see `output/validation_report.md` for what was excluded and why.

## The five metrics

| # | Metric | Value | n | How it is computed | What it tells the ops manager |
|---|---|---|---|---|---|
| 1 | **On-Time Departure Rate** (KPI) | **91.6%** | 6,430 | Departures with est. off-block ≤ STD + 15 min (DGCA rule). Est. off-block = OpenSky wheels-up − 15 min taxi (A-2). SQL check: 91.6%. | About 1 in 12 departures is late. Range 67–95% across taxi allowances 0–25 min (see sensitivity). |
| 2 | Average departure delay | +0.3 min (median -4.6) | 6,430 | Mean of est. off-block − STD over the same departures | The typical flight leaves on time (median below zero). The mean is pulled up by a tail: the 539 delayed flights average 60 min late. |
| 3 | Average arrival delay | -3.4 min (median -12.0) | 5,966 | Est. in-block (wheels-down + 8 min) − STA | Inbound flights mostly arrive ahead of schedule (89% within 15 min), so late arrivals are the exception rather than the rule. |
| 4 | Median aircraft turnaround | 108 min runway-to-runway | 9,276 turns | Same icao24: BLR wheels-down → next BLR wheels-up, 20 min–12 h (A-6) | ≈ 85 min gate-to-gate vs a scheduled 75 min; 72% of turns ran over schedule (median overrun +9 min, n=4,765). |
| 5 | Weather-impacted delay rate | 17.8% | 539 delayed | Delayed departures with an adverse hour in [STD − 1 h, departure]. Adverse = rain ≥ 1.0 mm/h, gusts ≥ 55 km/h, vis < 3000 m or thunderstorm (A-5) | Weather hurts when it happens (OTP 84% exposed vs 92% clear) but only 38 of 744 hours were adverse, so it explains a minority of delay. (The attribution below shows 12%, because a delay that is both inbound-late and weather-exposed counts as reactionary first.) |

## Where the delay comes from (attribution of 539 delayed departures)

Precedence follows DGCA (reactionary first); see `diagrams/workflow_model.md`.

| Attribution | Delayed departures | Share |
|---|---|---|
| ground-side / other | 319 | 59.2% |
| reactionary (inbound late) | 153 | 28.4% |
| weather-exposed | 67 | 12.4% |

| Airline group | Departures | OTP | Avg delay (min) |
|---|---|---|---|
| IndiGo | 4,187 | 92.9% | -0.7 |
| Air India Group | 1,509 | 88.5% | +1.0 |
| Akasa Air | 464 | 95.3% | +1.3 |
| Other | 201 | 84.1% | +6.6 |
| Alliance Air | 69 | 78.3% | +19.3 |

| Delayed departures by group: count (per 100 departures) | Reactionary | Weather-exposed | Ground-side / other | Inbound flights > 15 min late |
|---|---|---|---|---|
| Air India Group | 78 (5.2) | 13 (0.9) | 82 (5.4) | 15% of 923 |
| Akasa Air | 2 (0.4) | 1 (0.2) | 19 (4.1) | 11% of 253 |
| Alliance Air | 6 (8.7) | 0 (0.0) | 9 (13.0) | 24% of 63 |
| IndiGo | 54 (1.3) | 53 (1.3) | 190 (4.5) | 7% of 3,050 |
| Other | 13 (6.5) | 0 (0.0) | 19 (9.5) | n/a (no schedule) |

## Decision supported

**Lead with turnaround staffing / ground process.** Of delayed departures, 59% were ground-side (the aircraft was at the gate in time and the weather was clear, yet it still left late), against 41% inbound-late or weather-exposed. The strongest BLR-specific evidence is the inbound side: arriving flights reach the gate a median 12 min **early** (89% within 15 min), so most aircraft are available in time, and the delay is added on the ground.

Ground-side delay is **airport-wide, not one airline's**: every airline group with 200+ departures loses 4.1–5.4 departures per 100 to it, and IndiGo alone accounts for 190 of the 319 ground-side cases.

Of the airline groups with 200+ departures, Air India Group has the lowest OTP: 88.5% on 1,509 departures vs Akasa Air 95.3% at the same airport in the same weather (DGCA's own BLR figures show the same order). Most of that gap is **late-arriving aircraft, not the BLR turn**: per 100 departures it has 5.2 reactionary delays against 0.4 at Akasa Air and 1.3 at IndiGo, and 5.4 ground-side against 4.1 and 4.5. Its inbound flights reach BLR more than 15 min late 15% of the time, against 11% and 7%. So the airline liaison team's conversation with Air India Group is about inbound punctuality and buffers on its late-running rotations, not BLR ground staff.

**Where this could be wrong:** DGCA's *national* delay-cause split is 65% reactionary. Our reactionary test is stricter (the aircraft must have been physically unable to make STD), and the national figure includes the most congested hubs (Delhi, Mumbai), which carry the most flights. If BLR's true split looked like the national one, padding would win. The same happens if BLR's typical taxi-out were longer than 20 min (see the sensitivity table below). Asking the two largest airlines for their BLR-coded delay reasons would settle it; see `output/benchmark_comparison.md`.

Schedule padding would mainly help the reactionary share (28%). The weather-exposed share (12%) is too small in August to justify padding by itself. Benchmark against DGCA: `output/benchmark_comparison.md`.

## Sensitivity to the one assumption we could not measure (taxi-out allowance, A-2)

| Taxi-out allowance | OTP | Ground-side share | Reactionary share | Weather share | Airline OTP ranking |
|---|---|---|---|---|---|
| 0 min | 67.2% | 81% | 7% | 12% | IndiGo > Air India Group > Akasa Air > Alliance Air |
| 5 min | 80.2% | 75% | 12% | 13% | IndiGo > Air India Group > Akasa Air > Alliance Air |
| 10 min | 87.6% | 68% | 19% | 13% | Akasa Air > IndiGo > Air India Group > Alliance Air |
| 15 min | 91.6% | 59% | 28% | 12% | Akasa Air > IndiGo > Air India Group > Alliance Air |
| 20 min | 93.7% | 53% | 38% | 9% | Akasa Air > IndiGo > Air India Group > Alliance Air |
| 25 min | 95.0% | 44% | 48% | 8% | Akasa Air > IndiGo > Air India Group > Alliance Air |

Ground-side / other is the largest delay cause for **every allowance up to 20 min**. At 25 min, reactionary (inbound late) overtakes it (48% vs 44%). The airline ranking (Akasa Air > IndiGo > Air India Group > Alliance Air) holds for every allowance of 10 min or more. **So the recommendation holds for any taxi-out between 10 and 20 min.** The allowance that reproduces DGCA's published OTP is in `output/benchmark_comparison.md`. The OTP *level* moves with the allowance; the decision only changes outside this window.

**Retimes (V-DR-5):** 429 legs on 20 flight codes were offset from the September schedule on almost every August operation. Either the offset was large, or it was steady while the September sample showed the same flight on time. That is the signature of a schedule change between August and the September sample, not of delay. Excluding them moves OTP from 88.2% to 91.6%. Most were Air India Group (365 legs); left in, they would have exaggerated that group's delay problem.

## Known / Unknown / Assumption / Limitation (brief)

Full register, with IDs and evidence: `docs/known_unknown_assumptions.md`.

**Known**
- DGCA publishes BLR On-Time Performance = 92.0% for 2026-08. SkyPulse's like-for-like comparison is in `output/benchmark_comparison.md` (K-1).
- AviationStack's `+00:00` timestamps are local IST. On 483 matched flights, read as IST they sit a median +1.4 min (departures) and -1.6 min (arrivals) from OpenSky; read as UTC they are 329 min off (K-7).
- Indian carriers' radio callsigns differ from flight numbers. A mapping learned by matching aircraft and time lifts schedule coverage of the five airline groups' departures from 43.8% to 76.7% (K-9, K-11).

**Unknown**
- True gate (off-block / in-block) times: no public source has them (U-1).
- Cancelled flights, which never appear in radar data (U-2), and per-flight delay reasons, which aren't published. So "ground-side / other" is a residual that also contains ATC, crew and passenger-driven delay (U-4).

**Assumption**
- Gate time = runway time ∓ taxi allowance (15 min out, 8 min in). The decision holds for any taxi-out between 10 and 20 min (A-2).
- September schedules apply to August (A-3). The 429 legs whose schedule changed are excluded (V-DR-5). OTP by schedule source: callsign_map 93.2% (n=2,723), flight_code 90.4% (n=3,707).
- Adverse weather = rain ≥ 1.0 mm/h, gusts ≥ 55 km/h, visibility < 3000 m or a thunderstorm (A-5). Reactionary = the inbound aircraft reached the gate too late for a 30-min turn (A-8).

**Limitation**
- 23% of the five airline groups' departures have no schedule match. They count in traffic and turnarounds, not in delay metrics (L-6).
- DGCA's figures are airline self-reported (L-3); weather is modelled, not observed (L-4); the window is one monsoon month (L-5).
