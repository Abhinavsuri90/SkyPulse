# Validation report

_Generated 2026-09-26 14:20 UTC by `src/validate.py` from the current raw snapshot._

Rows are **flagged, never deleted**. `exclude` keeps the row in the database but out of KPI maths.

## Rule results

| Rule | Severity | Check | Flagged | % of rows | Why it matters |
|---|---|---|---|---|---|
| V-OS-1 | exclude | icao24 is a 6-char lowercase hex address | 0 | 0 | Aircraft identity is the join key for turnarounds |
| V-OS-2 | exclude | lastSeen after firstSeen | 0 | 0 | A leg cannot end before it starts |
| V-OS-3 | exclude | no duplicate leg (direction, icao24, firstSeen) | 0 | 0 | Duplicates would double-count flights |
| V-OS-4 | exclude | callsign follows the airline pattern AAA9... | 115 | 0.6% | Private / blank callsigns are not scheduled flights and have no schedule |
| V-OS-5 | info | callsign prefix is in the config airline map | 2,860 | 13.9% | Unmapped airlines cannot be rolled up to a DGCA group |
| V-OS-6 | exclude | first/last detection within 20 km and 1,500 m of BLR | 99 | 0.5% | A far detection means the timestamp is minutes off the real take-off/landing |
| V-OS-7 | exclude | not a BLR->BLR leg (circuit, or round trip merged by OpenSky) | 545 | 2.7% | Merged round trips carry the OUTBOUND callsign on the inbound end |
| V-OS-8 | info | other-end airport is known | 9,491 | 46.2% | Needed for domestic/international split; falls back to the schedule |
| V-AS-1 | exclude | row is the operating flight, not a codeshare duplicate | 339 | 22.8% | Codeshares repeat one physical flight under partner codes |
| V-AS-2 | exclude | one row per (direction, flight_date, operating flight) | 16 | 1.1% | Paging a live feed can return the same flight twice |
| V-AS-3 | info | icao24 normalised to lowercase | 958 | 64.5% | AviationStack sends UPPERCASE, OpenSky lowercase: a case-sensitive join silently loses ~70% of matches |
| V-AS-4 | info | '+00:00' timestamps re-read as local IST | 1,486 | 100.0% | AviationStack labels local times as UTC (proven in model.py cross-check) |
| V-AS-5 | exclude | scheduled time present | 0 | 0 | No schedule = nothing to measure delay against |
| V-AS-6 | info | flight not cancelled | 18 | 1.2% | Cancelled flights keep their schedule but have no actual time |
| V-WX-1 | critical | exactly 24 hourly rows per day in the window | 0 | 0 | Missing hours would silently under-count weather exposure |
| V-WX-2 | critical | no null values in the adverse-weather variables | 0 | 0 | A null hour cannot be classified |
| V-WX-3 | exclude | values physically plausible (precip>=0, gust 0-250 km/h, visibility>=0) | 0 | 0 | Sensor/model glitches must not trigger 'adverse' |
| V-AP-1 | critical | BLR present as VOBL / BLR / IN | 0 | 0 | The whole project is anchored on this row |
| V-AP-2 | info | airport codes seen in flight legs resolve in airports.csv | 4 |  | Unresolved codes cannot be classified domestic/international |
| V-DR-1 | exclude | leg matched to a schedule (flight code or learned callsign mapping) | 6,749 | 32.8% | No schedule, no delay; the leg is still counted in traffic and turnarounds |
| V-DR-2 | exclude | computed delay within [-60, +720] min | 254 | 1.2% | Outside this, the schedule match is almost certainly the wrong rotation/day |
| V-DR-3 | info | callsign->flight mapping is unambiguous | 0 | 0 | Ambiguous mappings are not used |
| V-DR-4 | info | turnaround between 20 min and 12 h | 399 | 4.1% | Shorter = artefact; longer = parked overnight, not an operational turn |
| V-DR-5 | exclude | flight code is not systematically offset from its schedule (late on >=80% of >=5 ops with a median > 60 min, or a stable offset while the Sept sample shows it on time; or early the same way) | 429 | 2.1% | That signature is a schedule change between August and the Sept sample (a retime), not a delay |

## Profiles

**stg_opensky**: 21,183 rows × 15 columns. Null %: {'est_dep_airport': 23.8, 'est_arr_airport': 21.0, 'dep_horiz_m': 23.8, 'dep_vert_m': 23.8, 'arr_horiz_m': 21.0, 'arr_vert_m': 21.0}. Distinct (first 12 cols): {'leg_id': 21183, 'direction': 2, 'icao24': 1245, 'callsign': 1291, 'first_seen': 20553, 'last_seen': 20542, 'est_dep_airport': 97, 'est_arr_airport': 119, 'dep_horiz_m': 5612, 'dep_vert_m': 842, 'arr_horiz_m': 6159, 'arr_vert_m': 916}

**stg_aviationstack**: 1,486 rows × 18 columns. Null %: {'status': 0.9, 'actual_local': 24.8, 'actual_runway_local': 24.8, 'icao24_raw': 1.4, 'icao24': 1.4}. Distinct (first 12 cols): {'as_id': 1486, 'direction': 2, 'flight_date': 3, 'status': 4, 'flight_icao': 837, 'flight_iata': 837, 'is_codeshare': 2, 'op_flight_icao': 639, 'airline_icao': 24, 'sched_local': 499, 'actual_local': 825, 'actual_runway_local': 825}

**stg_weather**: 744 rows × 7 columns. Null %: none. Distinct (first 12 cols): {'hour_local': 744, 'precipitation': 28, 'rain': 15, 'wind_speed_10m': 160, 'wind_gusts_10m': 94, 'visibility': 626, 'weather_code': 10}

