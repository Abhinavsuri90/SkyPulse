# Benchmark: SkyPulse OTP vs DGCA's published OTP for BLR (2026-08)

_Generated 2026-09-26 14:20 UTC by `src/benchmark.py`._

**Why this exists:** a pipeline that only checks its own maths is marking its own homework. DGCA, India's aviation regulator, publishes an OTP for BLR every month. We never use it to compute anything. It is only the independent number we test ourselves against.

## Headline

| | OTP, BLR domestic departures, 2026-08 |
|---|---|
| **DGCA published** (airline-reported, 5 airline groups) | **92.0%** |
| **SkyPulse computed** (OpenSky-observed, same 5 groups, domestic only, n = 5,738) | **91.8%** |
| Gap | -0.2 points |
| SkyPulse with the 236 retimed departures left in (see *What the headline depends on*) | 88.3% (gap -3.7 points) |

Same month, same airport, same airline groups, domestic flights only, same rule (delayed = more than 15 min after scheduled departure). The two numbers **agree closely: 0.2 points apart**.

## By airline group at BLR

| Airline group | SkyPulse departures | SkyPulse OTP | DGCA OTP | Gap (pts) |
|---|---|---|---|---|
| Akasa Air | 405 | 96.3% | 96.4% | -0.1 |
| IndiGo | 3,861 | 92.6% | 94.7% | -2.1 |
| Air India Group | 1,403 | 88.8% | 87.3% | +1.5 |
| Alliance Air | 69 | 78.3% | 76.8% | +1.5 |
| SpiceJet |  | not observed | 18.2% |  |

**Airline ranking: identical** between SkyPulse and DGCA. Two independent methods (radar-observed times vs airline self-reporting) put the airlines in the same order. That is strong evidence that the pipeline measures the right thing, even where the level differs.

## Why the numbers differ (plain English)

1. **Gate time vs runway time (the biggest unknown).** DGCA counts a flight as departed when the airline reports it left the gate (off-block). OpenSky sees the aircraft only once it is airborne. We subtract a 15-minute taxi allowance (assumption A-2), which no source available to us can measure (K-8). OTP is very sensitive to that allowance: 0 min → 67.9% | 5 min → 80.8% | 10 min → 87.9% | 15 min → 91.8% | 20 min → 93.9% | 25 min → 95.3% | 30 min → 96.2%. Our number would equal DGCA's at a taxi-out allowance of **16 minutes**. If BLR's real average taxi-out is around that, the taxi assumption alone explains the whole gap. That average is exactly what public data cannot tell us (U-1). That allowance is inside the range where the recommendation holds (10–20 min, see the evidence table), so the benchmark-consistent reading supports the same decision. We report this as a diagnostic and **deliberately do not tune the allowance to hit DGCA's figure**; doing that would turn the benchmark into an input.
2. **Self-reported vs observed.** DGCA's figure is compiled from what each airline reports, and the report itself says so on every page. Ours is observed independently. Neither is audited against the other, and some difference is expected.
3. **Population.** We can measure only departures we could match to a real schedule: 5,738 domestic departures, about three quarters of the 5 groups' BLR flights (L-6). We also exclude flight codes whose schedule changed after August (V-DR-5). Left in, they would widen the gap to -3.7 points; see *What the headline depends on* below. DGCA covers every flight the airlines operated.
4. **SpiceJet.** DGCA lists SpiceJet at BLR at 18.2% OTP. OpenSky saw only 32 SpiceJet departures from BLR in all of August (0.4% of the 5 groups' departures). The September AviationStack sample had no SpiceJet schedules, so SpiceJet is outside our population. At that volume it barely moves DGCA's BLR figure.
5. **Schedule vintage.** Our schedules come from a September sample applied to August (A-3). Flights that were retimed are caught by V-DR-5; smaller retimes may remain and would bias individual flights either way.

## What the headline depends on: retimed flights (V-DR-5)

The close agreement depends on setting aside 236 departures on 10 flight codes as **retimes**. These are flights whose schedule changed between August and the September sample that our schedules come from. Measured against September times, a timetable change looks like a delay on every operation. This is the biggest judgment in the benchmark, so here is its effect:

| Benchmark population | SkyPulse OTP | Gap to DGCA |
|---|---|---|
| As published: retimes excluded | 91.8% | -0.2 points |
| Only the 3 stable-offset retimes left in (the least certain ones) | 90.6% | -1.4 points |
| All retimes left in | 88.3% | -3.7 points |

With every retime left in: Air India Group 79.5% (DGCA 87.3%), Akasa Air 96.3% (DGCA 96.4%), Alliance Air 63.3% (DGCA 76.8%), IndiGo 91.6% (DGCA 94.7%). The airline ranking is **still identical**, so the ranking does not depend on this rule; the overall level does.

**Why they are retimes, not delays.** Real delay varies from day to day. Each of these codes was off its September schedule on at least 80% of its (at least 5) August operations: late by more than 60 min at the median, early by more than 30 min, or late by a steady amount (IQR ≤ 30 min) while the September sample shows the same flight on time. For 18 of the 18 codes that also have September actuals, the September flight sits more than 15 min closer to its schedule than the typical August flight did (smallest difference: 23 min). The timetable moved, not the flight. 2 code(s) have no September actuals and were flagged on their August offset alone.

| Flight | Direction | August ops | August median offset (min) | Spread, IQR (min) | September sample offset (min) | Flagged as |
|---|---|---|---|---|---|---|
| AXB941 | DEP | 12 | +666 | 16 | +28 | large offset |
| AXB5313 | DEP | 15 | +652 | 29 | +44 | large offset |
| AXB2924 | DEP | 23 | +558 | 200 | +34 | large offset |
| AXB2001 | DEP | 31 | +292 | 15 | +4 | large offset |
| AXB2361 | DEP | 12 | +229 | 6 | +8 | large offset |
| AXB2362 | ARR | 7 | +227 | 11 | -20 | large offset |
| LLR517 | DEP | 21 | +198 | 51 | +6 | large offset |
| AXB5312 | ARR | 15 | +188 | 142 | -26 | large offset |
| AXB920 | ARR | 12 | +156 | 91 | no actuals | large offset |
| AXB2931 | DEP | 31 | +142 | 16 | +20 | large offset |
| AXB2932 | ARR | 26 | +142 | 30 | -1 | large offset |
| AXB921 | DEP | 13 | +117 | 33 | no actuals | large offset |
| AIC2485 | ARR | 31 | +98 | 29 | -10 | large offset |
| AXB1507 | ARR | 24 | +94 | 18 | -23 | large offset |
| IGO411 | DEP | 31 | +65 | 12 | +5 | large offset |
| AXB2875 | DEP | 30 | +58 | 19 | +2 | stable offset; on time in schedule sample |
| AIC132 | ARR | 30 | +54 | 23 | -6 | stable offset; on time in schedule sample |
| AIC2511 | DEP | 30 | +42 | 7 | +2 | stable offset; on time in schedule sample |
| IGO6032 | DEP | 12 | +40 | 6 | -1 | stable offset; on time in schedule sample |
| AXB1216 | ARR | 23 | -37 | 10 | -14 | early offset |

The thresholds live in `config.yaml` (`kpi.retime`). Anyone who disagrees with a code can change them and rerun.

## Delay causes: our inference vs DGCA's national split

| Bucket | SkyPulse (BLR, inferred) | DGCA (all 10 airports, airline-coded) |
|---|---|---|
| reactionary (inbound late) | 29% | 65% |
| weather-exposed | 14% | 5% |
| ground-side / other | 52% | 30% |
| inbound not observed | 5% | no such code |

DGCA's "Reactionary" covers any delay the airline attributes to the previous rotation. Ours is stricter: the inbound aircraft must have reached the gate too late to be turned in 30 minutes. We count a delay as ground-side only when the aircraft was provably at BLR in time; delays whose inbound aircraft OpenSky never saw are kept apart as "inbound not observed". So our reactionary share is expected to be lower, and our ground-side share higher. Both sources agree that **weather is a small share of delay**.

**This is the one place the benchmark challenges our conclusion, and we say so.** DGCA's split is national: all 10 airports, including congested Delhi and Mumbai, where knock-on delay is endemic. The BLR-specific evidence points the other way: inbound flights reached the gate a median 12 min early in August. We keep the ground-side recommendation, and label it with this caveat in the evidence table. The next step for the ops manager is to ask IndiGo and Air India Group for their **BLR-only** delay codes for August; that single request would confirm or overturn it.

## Checks on the benchmark itself

The benchmark is parsed from chart labels in a PDF, so the parse is validated too:

| Rule | Result | Check | Observed |
|---|---|---|---|
| V-DG-1 | PASS | BLR airport-level OTP present exactly once | 1 |
| V-DG-2 | PASS | every OTP value within 0-100 % | 0 |
| V-DG-3 | PASS | airline-wise overall chart has all 5 groups | 5 |
| V-DG-4 | FLAG | no duplicated airport labels within a chart | [{'airline_group': 'Alliance Air', 'airport': 'GAU', 'otp_pct': 92.0}, {'airline_group': 'Alliance Air', 'airport': 'GAU', 'otp_pct': 63.5}] |
| V-DG-5 | PASS | delay-reason shares sum to ~100 % | 100.0 |

V-DG-4 found a **defect in DGCA's own report**: the Alliance Air chart lists GAU twice (92.0 and 63.5). These values are flagged and none is used. BLR is unaffected. An authoritative source is still a source to check, not ground truth.
