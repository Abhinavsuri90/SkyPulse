"""Stage 3 - Model. Turn validated staging tables into the entity / event model of a BLR aircraft visit.

Builds (SQLite): dim_airport, dim_airline, dim_aircraft, schedule_ref, callsign_xwalk, flight_leg,
event, turnaround, weather_hour, departure_outcome. Derived-rule flags (V-DR-*) are appended to
validation_flag. See diagrams/workflow_model.md for the model and docs/known_unknown_assumptions.md
for every assumption referenced below (A-*, K-*).

    python src/model.py
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

from common import (
    DB_PATH,
    PROCESSED,
    SourceError,
    atomic_write,
    get_logger,
    load_config,
)

log = get_logger("model")


def to_local(epoch: pd.Series, zone: str) -> pd.Series:
    """UTC epoch seconds -> naive local datetime (IST has no DST, so naive local arithmetic is safe)."""
    return pd.to_datetime(epoch, unit="s", utc=True).dt.tz_convert(zone).dt.tz_localize(None)


def utc_offset_min(zone: str) -> float:
    """Offset of the airport's zone from UTC, in minutes (IST = +330)."""
    return pd.Timestamp.now(tz=zone).utcoffset().total_seconds() / 60


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(h))


def resolve_schedule(observed: pd.Series, hhmm: pd.Series) -> pd.Series:
    """Put a daily 'HH:MM' schedule on the calendar day that is nearest to the observed time.

    A 23:50 flight that leaves at 00:20 belongs to the previous day's 23:50, not the same day's.
    """
    base = observed.dt.normalize() + pd.to_timedelta(hhmm + ":00")
    diff = (observed - base).dt.total_seconds() / 60
    base = base.where(diff <= 720, base + pd.Timedelta(days=1))
    base = base.where(diff >= -720, base - pd.Timedelta(days=1))
    return base


def sample_punctuality(a: pd.DataFrame) -> pd.DataFrame:
    """Median (actual - scheduled) minutes per flight code in the AviationStack sample (where actuals exist)."""
    x = a[a.actual_local.notna()].copy()
    x["offset_min"] = (pd.to_datetime(x.actual_local) - pd.to_datetime(x.sched_local)).dt.total_seconds() / 60
    return x.groupby(["direction", "op_flight_icao"]).offset_min.median().rename("sample_offset_min").reset_index()


def detect_retimes(legs: pd.DataFrame, thr: float, rt: dict, sample: pd.DataFrame | None = None) -> pd.DataFrame:
    """Flight codes whose 'delay' is really a schedule change between August and the schedule sample (V-DR-5).

    Real delay varies day to day; a retime is late (or early) on nearly every operation. It is a retime when
    (a) the offset is large, or (b) it is stable AND the sample shows the same flight running on time, which
    contradicts a chronic-delay explanation.
    """
    per = legs.groupby(["direction", "op_flight_icao"]).delay_min.agg(
        n="size", med="median", late=lambda x: (x > thr).mean(), early=lambda x: (x < rt["median_early_min"]).mean(),
        iqr=lambda x: x.quantile(.75) - x.quantile(.25)).reset_index()
    if sample is None:
        sample = pd.DataFrame({"direction": pd.Series(dtype=str), "op_flight_icao": pd.Series(dtype=str),
                               "sample_offset_min": pd.Series(dtype=float)})
    per = per.merge(sample, on=["direction", "op_flight_icao"], how="left")
    mostly_late = per.late >= rt["share"]
    large = mostly_late & (per.med > rt["median_late_min"])
    contradicted = (mostly_late & (per.med > thr) & (per.iqr <= rt["stable_iqr_max"])
                    & (per.sample_offset_min.abs() <= rt["sample_on_time_max"]))
    early = (per.early >= rt["share"]) & (per.med < rt["median_early_min"])
    out = per[(per.n >= rt["min_ops"]) & (large | contradicted | early)].copy()
    out["reason"] = np.select([large.loc[out.index], contradicted.loc[out.index]],
                              ["large offset", "stable offset; on time in schedule sample"], "early offset")
    return out


def build_schedule_ref(a: pd.DataFrame) -> pd.DataFrame:
    """One scheduled local time per (direction, operating flight code), from the AviationStack sample (A-3)."""
    s = a.assign(hhmm=a.sched_local.str[11:16])
    g = s.groupby(["direction", "op_flight_icao"])
    ref = g.agg(sched_hhmm=("hhmm", lambda x: x.mode().iloc[0]), n_obs=("hhmm", "size"),
                n_distinct_times=("hhmm", "nunique"), other_icao=("other_icao", lambda x: x.mode().iloc[0] if x.notna().any() else None),
                flight_iata=("flight_iata", "first")).reset_index()
    return ref


def crosscheck(a: pd.DataFrame, o: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Match AviationStack rows to OpenSky legs on the cross-check days by aircraft (icao24) and time.

    Yields (1) the callsign -> flight-code mapping, (2) proof of AviationStack's time zone,
    (3) how AviationStack 'actual' relates to OpenSky wheels-up/down.
    """
    zone = cfg["airport"]["timezone"]
    days = cfg.get("crosscheck_days", [])
    ac = a[a.flight_date.isin(days) & a.icao24.notna()].copy()
    ac["ref"] = pd.to_datetime(ac.actual_local.fillna(ac.sched_local))
    ac["has_actual"] = ac.actual_local.notna()
    oc = o[o.is_crosscheck == 1].copy()
    oc["t"] = to_local(oc.first_seen.where(oc.direction == "DEP", oc.last_seen), zone)
    m = ac.merge(oc[["direction", "icao24", "callsign", "t", "leg_id"]], on=["direction", "icao24"])
    m["dt_min"] = (m.t - m.ref).dt.total_seconds() / 60
    m = m[m.dt_min.abs() <= cfg["validation"]["crosscheck_match_window_min"]]
    # one-to-one: best leg per flight, then best flight per leg
    m = m.loc[m.groupby(["direction", "op_flight_icao"]).dt_min.apply(lambda s: s.abs().idxmin())]
    m = m.loc[m.groupby("leg_id").dt_min.apply(lambda s: s.abs().idxmin())]
    act = m[m.has_actual]
    stats = {
        "n": len(act),
        "median_diff_if_ist_min": round(float(act.dt_min.median()), 1),
        "median_diff_if_utc_min": round(float(act.dt_min.median() - utc_offset_min(zone)), 1),
        "iqr_min": round(float(act.dt_min.quantile(.75) - act.dt_min.quantile(.25)), 1),
        "by_direction": {d: round(float(g.dt_min.median()), 1) for d, g in act.groupby("direction")},
        "match_rate": {d: round(len(m[m.direction == d]) / max(1, len(ac[ac.direction == d])), 3) for d in ("DEP", "ARR")},
    }
    xw = m[["direction", "callsign", "op_flight_icao", "dt_min"]].copy()
    return xw, stats


class Flagger:
    """Collects derived-rule hits (V-DR-*) for the validation_flag table."""

    def __init__(self):
        self.rows: list[dict] = []

    def __call__(self, entity: str, ids, rule: str, severity: str, detail: str = ""):
        self.rows.extend({"entity": entity, "entity_id": i, "rule": rule, "severity": severity, "detail": detail}
                         for i in ids)

    def excluded(self) -> set:
        return {r["entity_id"] for r in self.rows if r["severity"] == "exclude"}


def build_crosswalk(xw_raw: pd.DataFrame, flag: Flagger) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep only unambiguous callsign -> flight mappings (V-DR-3)."""
    amb = xw_raw.groupby(["direction", "callsign"]).op_flight_icao.nunique()
    amb = amb[amb > 1].reset_index()
    flag("callsign_xwalk", (amb.direction + ":" + amb.callsign).tolist(), "V-DR-3", "info", "maps to >1 flight code")
    xw = xw_raw.merge(amb[["direction", "callsign"]], how="left", indicator=True)
    xw = xw[xw._merge == "left_only"].drop(columns="_merge").drop_duplicates(["direction", "callsign"])
    xw["same_as_flight_code"] = (xw.callsign == xw.op_flight_icao).astype(int)
    return xw, amb


def build_flight_legs(o: pd.DataFrame, sched: pd.DataFrame, xw: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """One row per OpenSky movement at BLR in the window, with its schedule, estimated gate time and delay."""
    zone, amap = cfg["airport"]["timezone"], cfg["airlines"]
    L = o[o.is_crosscheck == 0].copy()
    dep = L.direction == "DEP"
    L["runway_local"] = to_local(L.first_seen.where(dep, L.last_seen), zone)                # A-1
    L["other_airport"] = L.est_arr_airport.where(dep, L.est_dep_airport)
    L["airline_prefix"] = L.callsign.str[:3]
    L["airline_name"] = L.airline_prefix.map({k: v["name"] for k, v in amap.items()})
    L["dgca_group"] = L.airline_prefix.map({k: v["dgca_group"] for k, v in amap.items()})

    # schedule match: the callsign IS the flight code, or a callsign mapping learned on the cross-check day (A-4)
    key = L.direction + "|" + L.callsign
    sched_key = set(sched.direction + "|" + sched.op_flight_icao)
    xw_map = dict(zip(xw.direction + "|" + xw.callsign, xw.op_flight_icao, strict=True))
    direct = key.isin(sched_key)
    via = ~direct & (L.direction + "|" + key.map(xw_map).fillna("")).isin(sched_key)
    L["op_flight_icao"] = np.where(direct, L.callsign, np.where(via, key.map(xw_map), None))
    L["schedule_source"] = np.where(direct, "flight_code", np.where(via, "callsign_map", None))
    L = L.merge(sched[["direction", "op_flight_icao", "sched_hhmm", "other_icao"]].rename(columns={"other_icao": "sched_other_icao"}),
                on=["direction", "op_flight_icao"], how="left")

    # runway time -> estimated gate time (A-2), then delay against the schedule placed on the nearest day
    taxi_out, taxi_in = cfg["taxi"]["default_taxi_out_min"], cfg["taxi"]["default_taxi_in_min"]
    allowance = pd.to_timedelta(np.where(L.direction == "DEP", -taxi_out, taxi_in), unit="min")
    L["gate_local_est"] = L.runway_local + allowance
    has = L.sched_hhmm.notna()
    L["sched_local"] = pd.NaT
    L.loc[has, "sched_local"] = resolve_schedule(L.loc[has, "gate_local_est"], L.loc[has, "sched_hhmm"])
    L["sched_local"] = pd.to_datetime(L.sched_local)
    L["delay_min"] = (L.gate_local_est - L.sched_local).dt.total_seconds() / 60
    return L


def apply_derived_rules(L: pd.DataFrame, os_excluded: set, cfg: dict, flag: Flagger,
                        sample: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """V-DR-1 (no schedule), V-DR-2 (implausible delay), V-DR-5 (retimes); then decide in_kpi.

    Returns the legs and the table of retimed flight codes.
    """
    has = L.sched_hhmm.notna()
    flag("flight_leg", L.leg_id[~has & ~L.leg_id.isin(os_excluded)], "V-DR-1", "exclude")
    lo, hi = cfg["kpi"]["delay_min_bound"], cfg["kpi"]["delay_max_bound"]
    out_of_bounds = has & ~L.delay_min.between(lo, hi)
    flag("flight_leg", L.leg_id[out_of_bounds], "V-DR-2", "exclude", "probable wrong rotation/day")
    ok = has & ~L.leg_id.isin(os_excluded) & ~out_of_bounds
    retimed = detect_retimes(L[ok], cfg["kpi"]["on_time_threshold_min"], cfg["kpi"]["retime"], sample)
    is_retimed = ok & (L.direction + "|" + L.op_flight_icao.fillna("")).isin(set(retimed.direction + "|" + retimed.op_flight_icao))
    flag("flight_leg", L.leg_id[is_retimed], "V-DR-5", "exclude", "systematic offset = schedule changed since August")
    L["retimed"] = is_retimed.astype(int)
    L["in_kpi"] = (~L.leg_id.isin(os_excluded | flag.excluded())).astype(int)
    return L, retimed


def classify_domestic(L: pd.DataFrame, ap: pd.DataFrame, cfg: dict) -> dict:
    """Domestic = other end in India. Schedule destination first (authoritative), then OpenSky's estimate.

    Also returns the out-of-sample check of the callsign mapping: does OpenSky's observed destination agree with
    the schedule's? OpenSky estimates the nearest airport (Juhu VAJJ for Mumbai VABB), so 'agree' = within
    config validation.dest_agreement_km.
    """
    country = dict(zip(ap.ident, ap.iso_country, strict=True))
    country.update({k: v for k, v in zip(ap.icao_code, ap.iso_country, strict=True) if k})
    L["other_country"] = L.sched_other_icao.fillna(L.other_airport).map(country)
    L["is_domestic"] = np.where(L.other_country.isna(), None, (L.other_country == cfg["airport"]["country"]).astype(float))
    coords = ap.set_index("ident")[["latitude_deg", "longitude_deg"]].astype(float)
    chk = L[L.sched_other_icao.notna() & L.other_airport.notna() & (L.other_airport != cfg["airport"]["icao"])]
    p1, p2 = coords.reindex(chk.sched_other_icao).values, coords.reindex(chk.other_airport).values
    km = haversine_km(p1[:, 0], p1[:, 1], p2[:, 0], p2[:, 1])
    return chk.assign(ok=km <= cfg["validation"]["dest_agreement_km"]).groupby("schedule_source").ok.mean().mul(100).round(1).to_dict()


def build_events(L: pd.DataFrame) -> pd.DataFrame:
    """The event log: every timestamped step of every visit, in order per aircraft."""
    ev = []
    for d, names in (("DEP", ("SCHEDULED_DEP", "OFF_BLOCK_EST", "WHEELS_UP")),
                     ("ARR", ("SCHEDULED_ARR", "IN_BLOCK_EST", "WHEELS_DOWN"))):
        x = L[L.direction == d]
        for name, col in zip(names, ("sched_local", "gate_local_est", "runway_local"), strict=True):
            ev.append(x[["leg_id", "icao24", col]].dropna().rename(columns={col: "ts_local"}).assign(event_type=name))
    event = pd.concat(ev, ignore_index=True).sort_values(["icao24", "ts_local"])
    event.insert(0, "event_id", range(1, len(event) + 1))
    return event


def build_turnarounds(L: pd.DataFrame, fl: pd.DataFrame, cfg: dict, flag: Flagger) -> pd.DataFrame:
    """Landing at BLR -> the same aircraft's next BLR take-off (A-6)."""
    circuit = set(fl[(fl.rule == "V-OS-7") & (fl.detail.str.startswith("circuit"))].entity_id)
    # Merged round-trip landings (V-OS-7 'merged') are real landings, so they stay in for turnarounds.
    unusable = set(fl[fl.rule.isin(["V-OS-1", "V-OS-2", "V-OS-3", "V-OS-4", "V-OS-6"])].entity_id) | circuit
    T = L[~L.leg_id.isin(unusable)].sort_values(["icao24", "runway_local"])
    nxt = T.groupby("icao24")[["direction", "leg_id", "runway_local", "sched_local"]].shift(-1)
    pairs = T[(T.direction == "ARR") & (nxt.direction == "DEP")]
    ta = pd.DataFrame({"icao24": pairs.icao24, "arr_leg_id": pairs.leg_id, "dep_leg_id": nxt.loc[pairs.index, "leg_id"],
                       "wheels_down_local": pairs.runway_local, "wheels_up_local": nxt.loc[pairs.index, "runway_local"],
                       "sched_arr_local": pairs.sched_local, "sched_dep_local": nxt.loc[pairs.index, "sched_local"]})
    ta["ground_min"] = (ta.wheels_up_local - ta.wheels_down_local).dt.total_seconds() / 60
    ta["sched_ground_min"] = (ta.sched_dep_local - ta.sched_arr_local).dt.total_seconds() / 60
    tmin, tmax = cfg["turnaround"]["min_minutes"], cfg["turnaround"]["max_hours"] * 60
    ta["valid"] = ta.ground_min.between(tmin, tmax).astype(int)
    ta.insert(0, "turnaround_id", ta.arr_leg_id + ">" + ta.dep_leg_id)
    flag("turnaround", ta.turnaround_id[ta.valid == 0], "V-DR-4", "info", "outside operational range (overnight/parked or artefact)")
    return ta


def build_weather_hours(w: pd.DataFrame, fl: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Classify every hour as adverse or not (A-5). Implausible hours (V-WX-3) stay unclassified."""
    wx = cfg["weather"]["adverse"]
    W = w.rename(columns={"precipitation": "precip_mm", "wind_gusts_10m": "gust_kmh", "visibility": "visibility_m"}).copy()
    W["hour_local"] = pd.to_datetime(W.hour_local)
    reasons = pd.DataFrame({
        "rain": W.precip_mm >= wx["precipitation_mm"], "gusts": W.gust_kmh >= wx["wind_gusts_kmh"],
        "low_vis": W.visibility_m < wx["visibility_m"], "thunderstorm": W.weather_code.isin(wx["thunderstorm_codes"])})
    W["adverse"] = reasons.any(axis=1).astype(int)
    W["adverse_reason"] = reasons.apply(lambda r: ",".join(r.index[r]), axis=1)
    W.loc[W.hour_local.astype(str).isin(set(fl[fl.rule == "V-WX-3"].entity_id)), "adverse"] = None
    return W


def build_departure_outcomes(L: pd.DataFrame, ta: pd.DataFrame, W: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """ON_TIME / DELAYED per the DGCA rule, then attribute each delay (reactionary > weather > ground-side).

    Ground-side needs positive evidence that the aircraft was available: its inbound landing was seen in time, or it
    had been parked at BLR longer than a turn. A delay whose inbound OpenSky never saw is 'inbound not observed'.
    """
    thr = cfg["kpi"]["on_time_threshold_min"]
    D = L[(L.direction == "DEP") & (L.in_kpi == 1)].copy()
    D["outcome"] = np.where(D.delay_min > thr, "DELAYED", "ON_TIME")

    adverse_hours = set(W.hour_local[W.adverse == 1])
    look = pd.Timedelta(hours=cfg["weather"]["lookback_hours"])

    def exposed(r) -> int:  # any adverse hour between STD - lookback and the (estimated) departure
        h = (r.sched_local - look).floor("h")
        while h <= r.gate_local_est:
            if h in adverse_hours:
                return 1
            h += pd.Timedelta(hours=1)
        return 0

    D["wx_exposed"] = D.apply(exposed, axis=1)
    inbound = ta[ta.valid == 1][["dep_leg_id", "arr_leg_id", "wheels_down_local"]]
    D = D.merge(inbound.rename(columns={"dep_leg_id": "leg_id", "arr_leg_id": "inbound_arr_leg_id"}), on="leg_id", how="left")
    legs = L.set_index("leg_id")
    D["inbound_arr_delay_min"] = D.inbound_arr_leg_id.map(legs.delay_min.where(legs.in_kpi == 1))
    # A-8: reactionary if even a minimum turn from the inbound's (estimated) in-block could not make STD + 15
    in_block = D.wheels_down_local + pd.Timedelta(minutes=cfg["taxi"]["default_taxi_in_min"])
    min_turn = pd.Timedelta(minutes=cfg["turnaround"]["min_turn_minutes"])
    D["inbound_late"] = (D.inbound_arr_leg_id.notna() & (in_block + min_turn > D.sched_local + pd.Timedelta(minutes=thr))).astype(int)
    # Parked longer than an operational turn (overnight): the aircraft was at BLR well before STD.
    parked = set(ta.dep_leg_id[ta.ground_min > cfg["turnaround"]["max_hours"] * 60])
    D["inbound_seen"] = (D.inbound_arr_leg_id.notna() | D.leg_id.isin(parked)).astype(int)
    D["attribution"] = np.select(
        [D.outcome == "ON_TIME", D.inbound_late == 1, D.wx_exposed == 1, D.inbound_seen == 0],
        ["n/a (on time)", "reactionary (inbound late)", "weather-exposed", "inbound not observed"], "ground-side / other")
    return D[["leg_id", "outcome", "delay_min", "wx_exposed", "inbound_arr_leg_id", "inbound_arr_delay_min",
              "inbound_late", "inbound_seen", "attribution"]].rename(columns={"leg_id": "dep_leg_id"})


def write_tables(con: sqlite3.Connection, tables: dict[str, pd.DataFrame]) -> None:
    for name, df in tables.items():
        df = df.copy()
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = df[c].dt.strftime("%Y-%m-%dT%H:%M:%S")
        df.to_sql(name, con, index=False, if_exists="replace")


LEG_COLS = ["leg_id", "direction", "icao24", "callsign", "airline_prefix", "airline_name", "dgca_group",
            "other_airport", "sched_other_icao", "other_country", "is_domestic", "op_flight_icao", "schedule_source",
            "runway_local", "gate_local_est", "sched_local", "delay_min", "retimed", "in_kpi", "local_day", "source_file"]


def run() -> dict:
    cfg = load_config()
    con = sqlite3.connect(DB_PATH)
    o = pd.read_sql("SELECT * FROM stg_opensky", con)
    a = pd.read_sql("SELECT * FROM stg_aviationstack", con)
    w = pd.read_sql("SELECT * FROM stg_weather", con)
    ap = pd.read_sql("SELECT ident, icao_code, iata_code, name, iso_country, type, latitude_deg, longitude_deg FROM stg_airports", con)
    fl = pd.read_sql("SELECT * FROM validation_flag", con)
    flag = Flagger()

    excl = fl[fl.severity == "exclude"]
    os_excluded = set(excl[excl.entity == "opensky_leg"].entity_id)
    as_ok = a[~a.as_id.isin(set(excl[excl.entity == "aviationstack_row"].entity_id))]
    log.info("inputs: %d OpenSky legs (%d excluded by source rules), %d usable AviationStack rows",
             len(o), len(os_excluded & set(o.leg_id)), len(as_ok))

    # 1. schedule reference + cross-check day (A-3, A-4, K-7, K-8, K-9)
    sched = build_schedule_ref(as_ok)
    xw_raw, tz_stats = crosscheck(as_ok, o[~o.leg_id.isin(os_excluded)], cfg)
    v = cfg["validation"]
    if tz_stats["n"] < v["crosscheck_min_matches"] or abs(tz_stats["median_diff_if_ist_min"]) > v["crosscheck_max_median_offset_min"]:
        raise SourceError(f"model: AviationStack/OpenSky cross-check failed ({tz_stats}); time-zone assumption not proven")
    log.info("cross-check: %d matched flights; OpenSky - AviationStack actual = %+.1f min if local vs %+.1f if UTC -> times are local",
             tz_stats["n"], tz_stats["median_diff_if_ist_min"], tz_stats["median_diff_if_utc_min"])
    xw, amb = build_crosswalk(xw_raw, flag)
    both = a[a.actual_local.notna() & a.actual_runway_local.notna()]
    as_actual_eq_rwy = 100 * float((both.actual_local == both.actual_runway_local).mean()) if len(both) else float("nan")

    # 2. flight legs, derived rules, domestic split
    L = build_flight_legs(o, sched, xw, cfg)
    L, retimed = apply_derived_rules(L, os_excluded, cfg, flag, sample_punctuality(as_ok))
    dest_agree = classify_domestic(L, ap, cfg)

    # 3. events, turnarounds, weather, outcomes
    event = build_events(L)
    ta = build_turnarounds(L, fl, cfg, flag)
    W = build_weather_hours(w, fl, cfg)
    outcome = build_departure_outcomes(L, ta, W, cfg)

    # 4. dimensions + write
    used_codes = set(L.other_airport.dropna()) | set(L.sched_other_icao.dropna()) | {cfg["airport"]["icao"]}
    dim_airport = ap[ap.ident.isin(used_codes)].drop(columns=["latitude_deg", "longitude_deg"]).rename(
        columns={"ident": "icao", "iata_code": "iata"})
    write_tables(con, {
        "schedule_ref": sched, "callsign_xwalk": xw, "flight_leg": L[LEG_COLS], "event": event, "turnaround": ta,
        "weather_hour": W, "departure_outcome": outcome, "dim_airport": dim_airport,
        "dim_airline": pd.DataFrame([{"prefix": k, **v} for k, v in cfg["airlines"].items()]),
        "dim_aircraft": L.groupby("icao24").size().rename("legs").reset_index()})
    pd.DataFrame(flag.rows, columns=fl.columns).to_sql("validation_flag", con, index=False, if_exists="append")
    con.commit()
    con.close()

    # 5. reconciliation + stats (row counts at every hop: a silent loss would show here)
    five = L.dgca_group.notna() & (L.direction == "DEP") & ~L.leg_id.isin(os_excluded)
    merged = fl[(fl.rule == "V-OS-7") & fl.detail.str.startswith("merged") & fl.entity_id.isin(set(L.leg_id))]
    stats = {
        "raw_window_legs": int((o.is_crosscheck == 0).sum()), "flight_leg_rows": len(L),
        "legs_in_kpi": {d: int(((L.direction == d) & (L.in_kpi == 1)).sum()) for d in ("DEP", "ARR")},
        "schedule_ref_rows": len(sched), "xwalk_pairs": len(xw), "xwalk_ambiguous": len(amb),
        "tz_check": tz_stats, "as_actual_equals_runway_pct": round(as_actual_eq_rwy, 1),
        "coverage": {"direct_pct": round(100 * float((L.schedule_source[five] == "flight_code").mean()), 1),
                     "with_xwalk_pct": round(100 * float(L.schedule_source[five].notna().mean()), 1),
                     "unmatched_pct": round(100 * float(L.schedule_source[five].isna().mean()), 1)},
        "dest_agreement_pct_by_source": dest_agree,
        "dep_dest_unknown_pct": round(100 * float(L.other_airport[L.direction == "DEP"].isna().mean()), 1),
        "domestic_unknown_pct": round(100 * float(outcome.dep_leg_id.map(L.set_index("leg_id").is_domestic).isna().mean()), 1),
        "merged_round_trips": len(merged),
        "turnarounds": {"pairs": len(ta), "valid": int(ta.valid.sum())},
        "retimed": {"flight_codes": len(retimed), "legs": int(L.retimed.sum()),
                    "legs_by_group": L[L.retimed == 1].groupby(L.dgca_group.fillna("Other")).size().to_dict(),
                    "by_reason": retimed.reason.value_counts().to_dict(),
                    "codes": retimed.round(1).to_dict("records")},
        "event_rows": len(event), "weather_adverse_hours": int((W.adverse == 1).sum()),
        "taxi_allowance_min": {"out": cfg["taxi"]["default_taxi_out_min"], "in": cfg["taxi"]["default_taxi_in_min"]},
    }
    if stats["flight_leg_rows"] != stats["raw_window_legs"]:
        raise SourceError(f"model: reconciliation failed, {stats['raw_window_legs']} raw legs -> {stats['flight_leg_rows']} modelled")
    atomic_write(PROCESSED / "model_stats.json", json.dumps(stats, indent=2, default=str))
    log.info("reconciliation OK: %d raw window legs = %d flight_leg rows; in KPI: %s", stats["raw_window_legs"],
             stats["flight_leg_rows"], stats["legs_in_kpi"])
    log.info("schedule coverage (5 DGCA groups, departures): direct %.1f%% -> with callsign map %.1f%%",
             stats["coverage"]["direct_pct"], stats["coverage"]["with_xwalk_pct"])
    log.info("destination agreement OpenSky vs schedule: %s | turnarounds %s | adverse hours %d",
             dest_agree, stats["turnarounds"], stats["weather_adverse_hours"])
    return stats


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
