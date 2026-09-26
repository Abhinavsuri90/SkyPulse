# Workflow model: entities, events, states, interventions, outcomes

## 1. The operational workflow being modelled

One aircraft's visit to BLR is the unit of work. It arrives on an inbound leg, is turned on the ground, and leaves on an outbound leg. Delay can enter at three points, and each one maps to a different intervention:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> InboundAirborne: inbound leg operating
    InboundAirborne --> OnGround: WHEELS_DOWN (OpenSky lastSeen), then IN_BLOCK est.
    OnGround --> OnGround: TURNAROUND (deboard, clean, fuel, board)
    OnGround --> TaxiOut: OFF_BLOCK est. vs SCHEDULED_DEP (STD)
    TaxiOut --> Departed: WHEELS_UP (OpenSky firstSeen)
    Departed --> [*]

    note right of InboundAirborne
      Delay entry point 1: INBOUND LATE
      (reactionary / rotation delay)
      Intervention: schedule padding
    end note
    note right of OnGround
      Delay entry point 2: GROUND-SIDE
      (slow turn, handling, boarding)
      Intervention: turnaround staffing
    end note
    note left of TaxiOut
      Delay entry point 3: WEATHER / ATC
      (adverse hour at BLR)
      Intervention: absorb via padding
    end note
```

**Outcome** for every departure: `ON_TIME` (off-block ≤ STD + 15 min) or `DELAYED` (the DGCA rule).
**Attribution** for every *delayed* departure, applied in DGCA's precedence (reactionary first):

```mermaid
flowchart TD
    D["Departure DELAYED<br/>(est. off-block > STD + 15)"] --> R{"Inbound aircraft (same icao24)<br/>reached the gate too late to make STD?<br/>in-block + min turn > STD"}
    R -- yes --> A1["REACTIONARY / INBOUND-LATE<br/>→ schedule padding"]
    R -- "no / no inbound seen" --> W{"Adverse weather hour at BLR<br/>between STD − 1 h and departure?"}
    W -- yes --> A2["WEATHER-EXPOSED<br/>→ schedule padding / accept"]
    W -- no --> S{"Aircraft provably at BLR in time?<br/>inbound landing seen, or parked > 12 h"}
    S -- yes --> A3["GROUND-SIDE OR OTHER<br/>aircraft was available, weather clear,<br/>still left late → turnaround staffing"]
    S -- no --> A4["INBOUND NOT OBSERVED<br/>OpenSky missed the landing<br/>→ not attributed (L-8)"]
```

The decision in the brief follows from the attribution split. If reactionary and weather-exposed delays dominate, pad the schedule. If ground-side dominates, staff the turnaround. This is **our inference** (U-4 in `docs/known_unknown_assumptions.md`). It is cross-checked against DGCA's national delay-cause split (K-6), which puts reactionary at 65 %.

## 2. Relational / event model (SQLite, built by `src/model.py`)

```mermaid
erDiagram
    dim_airport    ||--o{ flight_leg : "other end of leg"
    dim_airline    ||--o{ flight_leg : "operates (callsign prefix)"
    dim_aircraft   ||--o{ flight_leg : "flies (icao24)"
    schedule_ref   ||--o{ flight_leg : "gives STD/STA (op_flight_icao)"
    callsign_xwalk ||--o{ flight_leg : "maps callsign to flight code"
    flight_leg     ||--o{ event : "emits"
    flight_leg     ||--o| turnaround : "arrival leg"
    flight_leg     ||--o| turnaround : "departure leg"
    flight_leg     ||--o| departure_outcome : "DEP legs"
    weather_hour   ||--o{ departure_outcome : "exposure window"
    flight_leg     ||--o{ validation_flag : "flags (never deletes)"

    dim_airport {
        text icao PK
        text iata
        text name
        text iso_country
        text type
    }
    dim_airline {
        text prefix PK
        text name
        text dgca_group
    }
    dim_aircraft {
        text icao24 PK
        int legs
    }
    schedule_ref {
        text direction PK
        text op_flight_icao PK
        text sched_hhmm "local HH:MM"
        int  n_obs
        text other_icao
    }
    callsign_xwalk {
        text direction PK
        text callsign PK
        text op_flight_icao FK
        int  same_as_flight_code
    }
    flight_leg {
        text leg_id PK
        text direction "DEP | ARR"
        text icao24 FK
        text callsign
        text airline_prefix FK
        text dgca_group
        text other_airport FK
        int  is_domestic
        text op_flight_icao FK "NULL if no schedule match"
        text schedule_source "flight_code | callsign_map"
        text runway_local "wheels-up / wheels-down"
        text gate_local_est "runway -/+ taxi allowance"
        text sched_local
        real delay_min
        int  retimed "V-DR-5"
        int  in_kpi "1 = passes all exclusion rules"
    }
    event {
        int  event_id PK
        text leg_id FK
        text icao24
        text event_type "SCHEDULED_* | WHEELS_* | *_BLOCK_EST"
        text ts_local
    }
    turnaround {
        text turnaround_id PK
        text arr_leg_id FK
        text dep_leg_id FK
        text icao24
        real ground_min
        real sched_ground_min
        int  valid "20 min - 12 h"
    }
    departure_outcome {
        text dep_leg_id PK
        text outcome "ON_TIME | DELAYED"
        int  wx_exposed
        text inbound_arr_leg_id FK
        int  inbound_late
        int  inbound_seen
        text attribution
    }
    weather_hour {
        text hour_local PK
        real precip_mm
        real gust_kmh
        real visibility_m
        int  weather_code
        int  adverse
    }
    validation_flag {
        text entity "opensky_leg | flight_leg | ..."
        text entity_id "e.g. leg_id"
        text rule
        text severity
        text detail
    }
    dgca_otp {
        text month
        text chart
        text airline_group
        text airport
        real otp_pct
        int  dup_label
    }
```

**Grain rules:**
- `flight_leg`: one row per OpenSky-observed movement *at BLR*. A departure and the next airport's arrival of the same flight are different legs, because only the BLR end is in scope.
- `event`: one row per timestamped lifecycle event. It is what lets us replay any aircraft's day at BLR in order.
- `validation_flag` holds every rule hit, keyed by `entity` + `entity_id` so one table serves legs, schedule rows, weather hours and turnarounds. `flight_leg.in_kpi` is derived from it, so a flagged row is excluded from the metrics yet stays queryable.

## 3. How the five metrics map to the model

| # | Metric | Computed from | Tied to KPI because |
|---|---|---|---|
| 1 | **On-Time Departure Rate** (KPI) | `departure_outcome.outcome` for `flight_leg.in_kpi = 1` | It *is* the KPI |
| 2 | Average departure delay | `flight_leg.delay_min`, DEP | Severity behind the OTP rate: a 70 % OTP with 16-min delays ≠ 70 % with 90-min delays |
| 3 | Average arrival delay | `flight_leg.delay_min`, ARR | Late arrivals feed reactionary departure delay (entry point 1) |
| 4 | Median aircraft turnaround | `turnaround.ground_min` where `valid = 1` | Ground time is where entry point 2 lives |
| 5 | Weather-impacted delay rate | `departure_outcome.wx_exposed` among delayed departures | Size of entry point 3 |
