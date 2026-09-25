# Benchmark: SkyPulse OTP vs DGCA's published OTP for BLR (2026-08)

_Generated 2026-09-24 00:08 UTC by `src/benchmark.py`._

**Why this exists:** a pipeline that only checks its own maths is marking its own homework. DGCA, India's aviation regulator, publishes an OTP for BLR every month. We never use it to compute anything. It is only the independent number we test ourselves against.

## Headline

| | OTP, BLR domestic departures, 2026-08 |
|---|---|
| **DGCA published** (airline-reported, 5 airline groups) | **92.0%** |
| **SkyPulse computed** (OpenSky-observed, same 5 groups, domestic only, n = 5,738) | **91.8%** |
| Gap | -0.2 points |

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

1. **Gate time vs runway time (the biggest factor).** DGCA counts a flight as departed when the airline reports it left the gate (off-block). OpenSky sees the aircraft only once it is airborne. We subtract a 15-minute taxi allowance (assumption A-2), which no source available to us can measure (K-8). OTP is very sensitive to that allowance: 0 min → 67.9% | 5 min → 80.8% | 10 min → 87.9% | 15 min → 91.8% | 20 min → 93.9% | 25 min → 95.3% | 30 min → 96.2%. Our number would equal DGCA's at a taxi-out allowance of **16 minutes**. If BLR's real average taxi-out is around that, the taxi assumption alone explains the whole gap. That average is exactly what public data cannot tell us (U-1). That allowance is inside the range where the recommendation holds (10–20 min, see the evidence table), so the benchmark-consistent reading supports the same decision. We report this as a diagnostic and **deliberately do not tune the allowance to hit DGCA's figure**; doing that would turn the benchmark into an input.
2. **Self-reported vs observed.** DGCA's figure is compiled from what each airline reports, and the report itself says so on every page. Ours is observed independently. Neither is audited against the other, and some difference is expected.
3. **Population.** We can measure only departures we could match to a real schedule: 5,738 domestic departures, about three quarters of the 5 groups' BLR flights (L-6). We also exclude flight codes whose schedule changed after August (V-DR-5). DGCA covers every flight the airlines operated.
4. **SpiceJet.** DGCA lists SpiceJet at BLR (18.2% OTP). OpenSky saw only 32 SpiceJet departures from BLR in all of August (0.4% of the 5 groups' departures). The September AviationStack sample had none, so we have no schedule for them and SpiceJet is outside our population. At that volume it barely moves DGCA's BLR figure.
5. **Schedule vintage.** Our schedules come from a September sample applied to August (A-3). Flights that were retimed are caught by V-DR-5; smaller retimes (< 60 min) may remain and would bias individual flights either way.

## Delay causes: our inference vs DGCA's national split

| Bucket | SkyPulse (BLR, inferred) | DGCA (all 10 airports, airline-coded) |
|---|---|---|
| reactionary (inbound late) | 29% | 65% |
| weather-exposed | 14% | 5% |
| ground-side / other | 57% | 30% |

DGCA's "Reactionary" covers any delay the airline attributes to the previous rotation. Ours is stricter: the inbound aircraft must have reached the gate too late to be turned in 30 minutes. We also treat an aircraft that arrived in time but still left late as ground-side. So our reactionary share is expected to be lower, and our ground-side share higher. Both sources agree that **weather is a small share of delay**.

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

V-DG-4 found a **defect in DGCA's own report**: the Alliance Air chart lists GAU twice with different values (92.0 and 63.5). Both values are flagged and neither is used. BLR is unaffected. An FDE treats an authoritative source as a source to check, not as ground truth.
