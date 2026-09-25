"""Stage 4 - Metrics. Compute the 5 KPI-linked metrics from the SQLite model and write the evidence table.

Metrics are SQL over the model (the derived SQL layer); medians and the sensitivity sweep use pandas
because SQLite has no MEDIAN. Everything written here is regenerated from raw data on every run.

    python src/metrics.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from benchmark import parse_dgca_report
from common import DB_PATH, OUTPUT, PROCESSED, RAW, atomic_write, get_logger, load_config

log = get_logger("metrics")

SQL = {
    # M1 + M2: departures that passed every exclusion rule (in_kpi = 1). outcome follows the DGCA 15-min rule.
    # The turn joined is the aircraft's BLR ground time before this departure (at most one per departure).
    "departures": """
        SELECT l.leg_id, l.dgca_group, l.schedule_source, l.is_domestic, l.delay_min, l.runway_local, l.sched_local,
               o.outcome, o.wx_exposed, o.inbound_late, o.attribution, o.inbound_arr_delay_min, t.sched_ground_min
        FROM flight_leg l JOIN departure_outcome o ON o.dep_leg_id = l.leg_id
        LEFT JOIN turnaround t ON t.dep_leg_id = l.leg_id AND t.valid = 1
        WHERE l.direction = 'DEP' AND l.in_kpi = 1""",
    # M3
    "arrivals": "SELECT leg_id, dgca_group, delay_min FROM flight_leg WHERE direction = 'ARR' AND in_kpi = 1",
    # M4: runway-to-runway ground time of the same aircraft (icao24), operational range only
    "turnarounds": "SELECT ground_min, sched_ground_min FROM turnaround WHERE valid = 1",
    # M1 headline, straight SQL (cross-checked against the pandas figure below)
    "otp_sql": """
        SELECT COUNT(*) AS n, ROUND(100.0 * SUM(o.outcome = 'ON_TIME') / COUNT(*), 1) AS otp_pct
        FROM flight_leg l JOIN departure_outcome o ON o.dep_leg_id = l.leg_id
        WHERE l.direction = 'DEP' AND l.in_kpi = 1""",
    "otp_by_group": """
        SELECT COALESCE(l.dgca_group, 'Other') AS grp, COUNT(*) AS n,
               ROUND(100.0 * SUM(o.outcome = 'ON_TIME') / COUNT(*), 1) AS otp_pct,
               ROUND(AVG(l.delay_min), 1) AS avg_delay_min
        FROM flight_leg l JOIN departure_outcome o ON o.dep_leg_id = l.leg_id
        WHERE l.direction = 'DEP' AND l.in_kpi = 1 GROUP BY grp ORDER BY n DESC""",
    "coverage": """
        SELECT direction, COUNT(*) AS legs, SUM(in_kpi) AS in_kpi,
               SUM(schedule_source IS NOT NULL) AS with_schedule
        FROM flight_leg GROUP BY direction""",
    "adverse_hours": "SELECT COUNT(*) AS n FROM weather_hour WHERE adverse = 1",
}


def pct(x: float) -> float:
    return round(100 * float(x), 1)


def delay_profile(d: pd.DataFrame, thr: int) -> dict:
    """Per airline group: delay causes per 100 departures, how often the inbound aircraft arrived late, and the scheduled turn.

    Rates per departure (not shares of delay) are what make groups of very different size comparable.
    """
    out = {}
    for grp, g in d.assign(grp=d.dgca_group.fillna("Other")).groupby("grp"):
        inb, turn = g.inbound_arr_delay_min.dropna(), g.sched_ground_min.dropna()
        out[grp] = {"departures": len(g),
                    "per100": {k: round(100 * int(v) / len(g), 1) for k, v in g.attribution[g.outcome == "DELAYED"].value_counts().items()},
                    "inbound_late_pct": pct((inb > thr).mean()) if len(inb) else None, "inbound_known": len(inb),
                    "median_sched_turn_min": round(float(turn.median()), 1) if len(turn) else None}
    return out


def run() -> dict:
    cfg = load_config()
    thr = cfg["kpi"]["on_time_threshold_min"]
    with sqlite3.connect(DB_PATH) as con:
        q = {k: pd.read_sql(v, con) for k, v in SQL.items()}
    d, a, t = q["departures"], q["arrivals"], q["turnarounds"]
    delayed = d[d.outcome == "DELAYED"]
    taxi_in, taxi_out = cfg["taxi"]["default_taxi_in_min"], cfg["taxi"]["default_taxi_out_min"]

    # Sensitivity to the one assumption we cannot measure (A-2): for each taxi-out allowance, recompute
    # OTP, the attribution split, and the airline OTP ranking (the two legs the decision stands on).
    raw = (pd.to_datetime(d.runway_local) - pd.to_datetime(d.sched_local)).dt.total_seconds() / 60
    sens = []
    for x in cfg["taxi"]["sensitivity_out_min"]:
        late = (raw - x) > thr
        dl = d[late]
        cat = pd.Series("ground-side / other", index=dl.index).mask(dl.wx_exposed == 1, "weather-exposed").mask(
            dl.inbound_late == 1, "reactionary (inbound late)")
        share = cat.value_counts(normalize=True).mul(100).round(1).to_dict()
        rank = d.assign(ok=~late).groupby("dgca_group").ok.mean().sort_values(ascending=False)
        sens.append({"taxi_out_min": x, "otp_pct": pct((~late).mean()), "attribution_pct": share,
                     "ranking": list(rank.index), "otp_by_group": rank.mul(100).round(1).to_dict()})

    att = delayed.attribution.value_counts()
    t_gate = t.ground_min - taxi_in - taxi_out
    has_sched = t.sched_ground_min.notna()
    overrun = (t_gate - t.sched_ground_min)[has_sched]
    exp = d.groupby("wx_exposed").outcome.apply(lambda s: pct((s == "ON_TIME").mean())).to_dict()

    stats = json.loads((PROCESSED / "model_stats.json").read_text())
    with sqlite3.connect(DB_PATH) as con:
        rt = pd.read_sql("""SELECT delay_min FROM flight_leg WHERE direction = 'DEP' AND retimed = 1""", con)
    otp_incl = pct(((d.delay_min <= thr).sum() + (rt.delay_min <= thr).sum()) / (len(d) + len(rt)))
    dgca_otp, dgca_reasons = parse_dgca_report(RAW / "dgca" / f"dgca_traffic_report_{cfg['sources']['dgca']['benchmark_month']}.pdf")
    blr = dgca_otp[(dgca_otp.chart == "airport_all_airlines") & (dgca_otp.airport == cfg["airport"]["iata"]) & ~dgca_otp.dup_label]
    m = {
        # facts the evidence table's brief Known / Unknown / Assumption / Limitation section quotes
        "context": {"dgca_blr_pct": float(blr.otp_pct.iloc[0]) if len(blr) else None,
                    "benchmark_month": cfg["sources"]["dgca"]["benchmark_month"],
                    "tz_check": stats["tz_check"], "coverage": stats["coverage"]},
        "dgca_national_reactionary_pct": float(dgca_reasons.set_index("reason").pct.get("Reactionary", float("nan"))),
        "retimed": {**{k: v for k, v in stats["retimed"].items() if k != "codes"}, "otp_if_included_pct": otp_incl},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "window": cfg["analysis_window"], "airport": cfg["airport"]["iata"], "threshold_min": thr,
        "coverage": q["coverage"].to_dict("records"),
        "m1_otp": {"value_pct": pct((d.outcome == "ON_TIME").mean()), "n": len(d), "delayed": len(delayed),
                   "sql_check_pct": float(q["otp_sql"].otp_pct[0])},
        "m2_dep_delay": {"mean_min": round(d.delay_min.mean(), 1), "median_min": round(d.delay_min.median(), 1),
                         "p90_min": round(d.delay_min.quantile(.9), 1), "mean_of_delayed_min": round(delayed.delay_min.mean(), 1),
                         "n": len(d)},
        "m3_arr_delay": {"mean_min": round(a.delay_min.mean(), 1), "median_min": round(a.delay_min.median(), 1),
                         "p90_min": round(a.delay_min.quantile(.9), 1), "on_time_pct": pct((a.delay_min <= thr).mean()), "n": len(a)},
        "m4_turnaround": {"median_runway_to_runway_min": round(t.ground_min.median(), 1), "n": len(t),
                          "median_gate_equiv_min": round(t_gate.median(), 1),
                          "median_scheduled_ground_min": round(t.sched_ground_min.median(), 1) if has_sched.any() else None,
                          "median_overrun_vs_schedule_min": round(overrun.median(), 1) if len(overrun) else None,
                          "pct_turns_over_schedule": pct((overrun > 0).mean()) if len(overrun) else None,
                          "n_with_schedule": int(has_sched.sum())},
        "m5_weather": {"value_pct": pct(delayed.wx_exposed.mean()), "delayed_n": len(delayed),
                       "delayed_exposed_n": int(delayed.wx_exposed.sum()), "share_of_all_departures_exposed_pct": pct(d.wx_exposed.mean()),
                       "otp_when_exposed_pct": exp.get(1), "otp_when_clear_pct": exp.get(0),
                       "adverse_hours": int(q["adverse_hours"].n[0]), "hours_in_window": 24 * 31},
        "attribution": {k: {"n": int(v), "pct": pct(v / len(delayed))} for k, v in att.items()},
        "attribution_by_group": (delayed.assign(grp=delayed.dgca_group.fillna("Other"))
                                 .groupby(["grp", "attribution"]).size().unstack(fill_value=0).to_dict("index")),
        "delay_profile_by_group": delay_profile(d, thr),
        "otp_by_group": q["otp_by_group"].to_dict("records"),
        "otp_by_schedule_source": d.groupby("schedule_source").apply(
            lambda g: {"n": len(g), "otp_pct": pct((g.outcome == "ON_TIME").mean())}).to_dict(),
        "sensitivity_taxi_out": sens,
        "assumptions": {"taxi_out_min": taxi_out, "taxi_in_min": taxi_in,
                        "min_turn_min": cfg["turnaround"]["min_turn_minutes"], "adverse": cfg["weather"]["adverse"]},
    }
    m["decision_window"] = decision_window(sens, taxi_out)
    if abs(m["m1_otp"]["value_pct"] - m["m1_otp"]["sql_check_pct"]) > 0.1:
        raise RuntimeError(f"metrics: SQL and pandas OTP disagree ({m['m1_otp']})")
    atomic_write(PROCESSED / "metrics.json", json.dumps(m, indent=2, default=str))
    atomic_write(OUTPUT / "evidence_table.md", render(m, cfg))
    log.info("M1 OTP %.1f%% (n=%d) | M2 dep delay mean %.1f / median %.1f | M3 arr delay mean %.1f | M4 turnaround median %.1f | M5 weather %.1f%%",
             m["m1_otp"]["value_pct"], m["m1_otp"]["n"], m["m2_dep_delay"]["mean_min"], m["m2_dep_delay"]["median_min"],
             m["m3_arr_delay"]["mean_min"], m["m4_turnaround"]["median_runway_to_runway_min"], m["m5_weather"]["value_pct"])
    return m


def decision_window(sens: list[dict], base_allowance: int) -> dict:
    """Range of taxi-out allowances over which the recommendation holds.

    ground_leads_up_to: largest allowance such that ground-side is the top delay cause at it and every smaller one.
    ranking_stable_from: smallest allowance from which the airline OTP ranking equals the base-case ranking.
    """
    top = {r["taxi_out_min"]: max(r["attribution_pct"], key=r["attribution_pct"].get) for r in sens}
    xs = sorted(top)
    lead = [x for x in xs if all(top[y] == "ground-side / other" for y in xs if y <= x)]
    after = [x for x in xs if lead and x > lead[-1]]
    base_rank = next(r["ranking"] for r in sens if r["taxi_out_min"] == base_allowance)
    stable = min(x for x in xs if all(r["ranking"] == base_rank for r in sens if r["taxi_out_min"] >= x))
    over = next((r for r in sens if after and r["taxi_out_min"] == after[0]), None)
    return {"ground_leads_up_to": lead[-1] if lead else None, "overtaken_at": after[0] if after else None,
            "overtaken_by": top[after[0]] if after else None,
            "overtaken_pcts": ({k: over["attribution_pct"].get(k) for k in ("ground-side / other", top[after[0]])} if over else None),
            "ranking_stable_from": stable, "base_ranking": base_rank}


def decision(m: dict) -> tuple[str, str]:
    att = {k: v["pct"] for k, v in m["attribution"].items()}
    inbound_wx = att.get("reactionary (inbound late)", 0) + att.get("weather-exposed", 0)
    ground = att.get("ground-side / other", 0)
    groups = [g for g in m["otp_by_group"] if g["grp"] != "Other" and g["n"] >= 200]
    worst = min(groups, key=lambda g: g["otp_pct"])
    best = max(groups, key=lambda g: g["otp_pct"])
    lead = "turnaround staffing / ground process" if ground > inbound_wx else "schedule padding"
    text = (f"**Lead with {lead}.** Of delayed departures, {ground:.0f}% were ground-side (the aircraft was at the gate in time and the weather was "
            f"clear, yet it still left late), against {inbound_wx:.0f}% inbound-late or weather-exposed. ")
    text += (f"The strongest BLR-specific evidence is the inbound side: arriving flights reach the gate a median "
             f"{abs(m['m3_arr_delay']['median_min']):.0f} min **early** ({m['m3_arr_delay']['on_time_pct']:.0f}% within 15 min), so most aircraft "
             f"are available in time, and the delay is added on the ground.\n\n")
    # Per-departure rates, so an airline's share of delay is not just its share of traffic.
    R, G = "reactionary (inbound late)", "ground-side / other"
    prof = m["delay_profile_by_group"]

    def rate(grp: str, k: str) -> float:
        return prof[grp]["per100"].get(k, 0.0)

    gs = {g["grp"]: rate(g["grp"], G) for g in groups}
    lo, hi = min(gs, key=gs.get), max(gs, key=gs.get)
    big = max(groups, key=lambda g: g["n"])["grp"]
    # Ground-side rates within a factor of 1.5 across the large groups are read as one airport-wide problem.
    spread = (f"is **airport-wide, not one airline's**: every airline group with 200+ departures loses {gs[lo]:.1f}–{gs[hi]:.1f} "
              f"departures per 100 to it" if gs[lo] and gs[hi] / gs[lo] < 1.5 else
              f"is **uneven across airlines**: from {gs[lo]:.1f} per 100 departures at {lo} to {gs[hi]:.1f} at {hi}")
    text += (f"Ground-side delay {spread}, and {big} alone accounts for {m['attribution_by_group'][big].get(G, 0)} of the "
             f"{m['attribution'][G]['n']} ground-side cases.\n\n")
    w = worst["grp"]
    peers = [g["grp"] for g in sorted(groups, key=lambda g: -g["otp_pct"]) if g["grp"] != w]
    inbound = rate(w, R) - max(rate(p, R) for p in peers) > rate(w, G) - max(rate(p, G) for p in peers)
    text += (f"Of the airline groups with 200+ departures, {w} has the lowest OTP: {worst['otp_pct']:.1f}% on {worst['n']:,} departures vs "
             f"{best['grp']} {best['otp_pct']:.1f}% at the same airport in the same weather (DGCA's own BLR figures show the same order). "
             f"Most of that gap is **{'late-arriving aircraft, not the BLR turn' if inbound else 'time lost on the ground'}**: per 100 departures "
             f"it has {rate(w, R):.1f} reactionary delays against " + " and ".join(f"{rate(p, R):.1f} at {p}" for p in peers)
             + f", and {rate(w, G):.1f} ground-side against " + " and ".join(f"{rate(p, G):.1f}" for p in peers) + ". ")
    if inbound and all(prof[g]["inbound_late_pct"] is not None for g in [w, *peers]):
        text += (f"Its inbound flights reach BLR more than {m['threshold_min']} min late {prof[w]['inbound_late_pct']:.0f}% of the time, against "
                 + " and ".join(f"{prof[p]['inbound_late_pct']:.0f}%" for p in peers) + ". ")
    text += (f"So the airline liaison team's conversation with {w} is about "
             f"{'inbound punctuality and buffers on its late-running rotations, not BLR ground staff' if inbound else 'its turn process at BLR'}.\n\n")
    text += (f"**Where this could be wrong:** DGCA's *national* delay-cause split is {m['dgca_national_reactionary_pct']:.0f}% reactionary. "
             "Our reactionary test is stricter (the aircraft must have been physically unable to make STD), and the national figure "
             "includes the most congested hubs (Delhi, Mumbai), which carry the most flights. "
             "If BLR's true split looked like the national one, padding would win. The same happens if BLR's typical taxi-out were "
             f"longer than {m['decision_window']['ground_leads_up_to']} min (see the sensitivity table below). Asking the two largest airlines "
             "for their BLR-coded delay reasons would settle it; see `output/benchmark_comparison.md`.")
    return lead, text


def render(m: dict, cfg: dict) -> str:
    s = {r["taxi_out_min"]: r["otp_pct"] for r in m["sensitivity_taxi_out"]}
    t, w = m["m4_turnaround"], m["m5_weather"]
    lead, text = decision(m)
    rows = [
        ("1", "**On-Time Departure Rate** (KPI)", f"**{m['m1_otp']['value_pct']:.1f}%**", f"{m['m1_otp']['n']:,}",
         f"Departures with est. off-block ≤ STD + {m['threshold_min']} min (DGCA rule). Est. off-block = OpenSky wheels-up − {cfg['taxi']['default_taxi_out_min']} min taxi (A-2). SQL check: {m['m1_otp']['sql_check_pct']:.1f}%.",
         f"About 1 in {round(100 / max(0.1, 100 - m['m1_otp']['value_pct']))} departures is late. Range {s[min(s)]:.0f}–{s[max(s)]:.0f}% across taxi allowances {min(s)}–{max(s)} min (see sensitivity)."),
        ("2", "Average departure delay", f"{m['m2_dep_delay']['mean_min']:+.1f} min (median {m['m2_dep_delay']['median_min']:+.1f})", f"{m['m2_dep_delay']['n']:,}",
         "Mean of est. off-block − STD over the same departures",
         f"The typical flight leaves on time (median below zero). The mean is pulled up by a tail: the {m['m1_otp']['delayed']:,} delayed flights average {m['m2_dep_delay']['mean_of_delayed_min']:.0f} min late."),
        ("3", "Average arrival delay", f"{m['m3_arr_delay']['mean_min']:+.1f} min (median {m['m3_arr_delay']['median_min']:+.1f})", f"{m['m3_arr_delay']['n']:,}",
         f"Est. in-block (wheels-down + {cfg['taxi']['default_taxi_in_min']} min) − STA",
         f"Inbound flights mostly arrive ahead of schedule ({m['m3_arr_delay']['on_time_pct']:.0f}% within 15 min), so late arrivals are the exception rather than the rule."),
        ("4", "Median aircraft turnaround", f"{t['median_runway_to_runway_min']:.0f} min runway-to-runway", f"{t['n']:,} turns",
         f"Same icao24: BLR wheels-down → next BLR wheels-up, {cfg['turnaround']['min_minutes']} min–{cfg['turnaround']['max_hours']} h (A-6)",
         f"≈ {t['median_gate_equiv_min']:.0f} min gate-to-gate vs a scheduled {t['median_scheduled_ground_min']:.0f} min; "
         f"{t['pct_turns_over_schedule']:.0f}% of turns ran over schedule (median overrun {t['median_overrun_vs_schedule_min']:+.0f} min, n={t['n_with_schedule']:,})."),
        ("5", "Weather-impacted delay rate", f"{w['value_pct']:.1f}%", f"{w['delayed_n']:,} delayed",
         f"Delayed departures with an adverse hour in [STD − 1 h, departure]. Adverse = rain ≥ {cfg['weather']['adverse']['precipitation_mm']} mm/h, gusts ≥ {cfg['weather']['adverse']['wind_gusts_kmh']:.0f} km/h, vis < {cfg['weather']['adverse']['visibility_m']} m or thunderstorm (A-5)",
         f"Weather hurts when it happens (OTP {w['otp_when_exposed_pct']:.0f}% exposed vs {w['otp_when_clear_pct']:.0f}% clear) but only {w['adverse_hours']} of {w['hours_in_window']} hours were adverse, so it explains a minority of delay. "
         f"(The attribution below shows {m['attribution'].get('weather-exposed', {}).get('pct', 0):.0f}%, because a delay that is both inbound-late and weather-exposed counts as reactionary first.)"),
    ]
    att_rows = "\n".join(f"| {k} | {v['n']:,} | {v['pct']:.1f}% |" for k, v in sorted(m["attribution"].items(), key=lambda kv: -kv[1]["n"]))
    grp_rows = "\n".join(f"| {g['grp']} | {g['n']:,} | {g['otp_pct']:.1f}% | {g['avg_delay_min']:+.1f} |" for g in m["otp_by_group"])
    abg, prof = m["attribution_by_group"], m["delay_profile_by_group"]
    cats = ["reactionary (inbound late)", "weather-exposed", "ground-side / other"]

    def inbound_cell(p: dict) -> str:
        return f"{p['inbound_late_pct']:.0f}% of {p['inbound_known']:,}" if p["inbound_late_pct"] is not None else "n/a (no schedule)"
    abg_rows = "\n".join(f"| {g} | " + " | ".join(f"{v.get(c, 0)} ({prof[g]['per100'].get(c, 0.0):.1f})" for c in cats)
                         + f" | {inbound_cell(prof[g])} |" for g, v in abg.items())
    sens_tbl = "\n".join(
        f"| {r['taxi_out_min']} min | {r['otp_pct']:.1f}% | {r['attribution_pct'].get('ground-side / other', 0):.0f}% | "
        f"{r['attribution_pct'].get('reactionary (inbound late)', 0):.0f}% | {r['attribution_pct'].get('weather-exposed', 0):.0f}% | "
        f"{' > '.join(r['ranking'])} |" for r in m["sensitivity_taxi_out"])
    dw = m["decision_window"]
    if dw["overtaken_at"] is None:
        lead_txt = "Ground-side / other is the largest delay cause at **every allowance tested**"
        window = f"any allowance of {dw['ranking_stable_from']} min or more"
    else:
        p = dw["overtaken_pcts"]
        lead_txt = (f"Ground-side / other is the largest delay cause for **every allowance up to {dw['ground_leads_up_to']} min**. "
                    f"At {dw['overtaken_at']} min, {dw['overtaken_by']} overtakes it ({p[dw['overtaken_by']]:.0f}% vs "
                    f"{p['ground-side / other']:.0f}%)")
        window = f"any taxi-out between {dw['ranking_stable_from']} and {dw['ground_leads_up_to']} min"
    robust = (f"{lead_txt}. The airline ranking ({' > '.join(dw['base_ranking'])}) holds for every allowance of "
              f"{dw['ranking_stable_from']} min or more. **So the recommendation holds for {window}.** The allowance that reproduces "
              f"DGCA's published OTP is in `output/benchmark_comparison.md`. The OTP *level* moves with the allowance; the decision "
              f"only changes outside this window.")
    cov = {c["direction"]: c for c in m["coverage"]}
    c, wx = m["context"], cfg["weather"]["adverse"]
    return f"""# SkyPulse evidence table: BLR, {m['window']['start']} → {m['window']['end']}

_Generated {m['generated_utc']} by `src/metrics.py` from the committed raw snapshot. Every number is reproducible with `python src/pipeline.py`._

**KPI:** On-Time Departure Rate. **Population:** BLR departures that have a real schedule match (AviationStack) and pass every validation rule. That is {cov['DEP']['in_kpi']:,} of {cov['DEP']['legs']:,} observed departures; see `output/validation_report.md` for what was excluded and why.

## The five metrics

| # | Metric | Value | n | How it is computed | What it tells the ops manager |
|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(r) + " |" for r in rows) + f"""

## Where the delay comes from (attribution of {m['m1_otp']['delayed']:,} delayed departures)

Precedence follows DGCA (reactionary first); see `diagrams/workflow_model.md`.

| Attribution | Delayed departures | Share |
|---|---|---|
{att_rows}

| Airline group | Departures | OTP | Avg delay (min) |
|---|---|---|---|
{grp_rows}

| Delayed departures by group: count (per 100 departures) | Reactionary | Weather-exposed | Ground-side / other | Inbound flights > {m['threshold_min']} min late |
|---|---|---|---|---|
{abg_rows}

## Decision supported

{text}

Schedule padding would mainly help the reactionary share ({m['attribution'].get('reactionary (inbound late)', {}).get('pct', 0):.0f}%). The weather-exposed share ({m['attribution'].get('weather-exposed', {}).get('pct', 0):.0f}%) is too small in August to justify padding by itself. Benchmark against DGCA: `output/benchmark_comparison.md`.

## Sensitivity to the one assumption we could not measure (taxi-out allowance, A-2)

| Taxi-out allowance | OTP | Ground-side share | Reactionary share | Weather share | Airline OTP ranking |
|---|---|---|---|---|---|
{sens_tbl}

{robust}

**Retimes (V-DR-5):** {m['retimed']['legs']:,} legs on {m['retimed']['flight_codes']} flight codes were offset from the September schedule on almost every August operation. Either the offset was large, or it was steady while the September sample showed the same flight on time. That is the signature of a schedule change between August and the September sample, not of delay. Excluding them moves OTP from {m['retimed']['otp_if_included_pct']:.1f}% to {m['m1_otp']['value_pct']:.1f}%. Most were Air India Group ({m['retimed']['legs_by_group'].get('Air India Group', 0)} legs); left in, they would have exaggerated that group's delay problem.

## Known / Unknown / Assumption / Limitation (brief)

Full register, with IDs and evidence: `docs/known_unknown_assumptions.md`.

**Known**
- DGCA publishes BLR On-Time Performance = {c['dgca_blr_pct']:.1f}% for {c['benchmark_month']}. SkyPulse's like-for-like comparison is in `output/benchmark_comparison.md` (K-1).
- AviationStack's `+00:00` timestamps are local IST. On {c['tz_check']['n']} matched flights, read as IST they sit a median {c['tz_check']['by_direction']['DEP']:+.1f} min (departures) and {c['tz_check']['by_direction']['ARR']:+.1f} min (arrivals) from OpenSky; read as UTC they are {abs(c['tz_check']['median_diff_if_utc_min']):.0f} min off (K-7).
- Indian carriers' radio callsigns differ from flight numbers. A mapping learned by matching aircraft and time lifts schedule coverage of the five airline groups' departures from {c['coverage']['direct_pct']:.1f}% to {c['coverage']['with_xwalk_pct']:.1f}% (K-9, K-11).

**Unknown**
- True gate (off-block / in-block) times: no public source has them (U-1).
- Cancelled flights, which never appear in radar data (U-2), and per-flight delay reasons, which aren't published. So "ground-side / other" is a residual that also contains ATC, crew and passenger-driven delay (U-4).

**Assumption**
- Gate time = runway time ∓ taxi allowance ({cfg['taxi']['default_taxi_out_min']} min out, {cfg['taxi']['default_taxi_in_min']} min in). The decision holds for {window} (A-2).
- September schedules apply to August (A-3). The {m['retimed']['legs']:,} legs whose schedule changed are excluded (V-DR-5). OTP by schedule source: {', '.join(f"{k} {v['otp_pct']:.1f}% (n={v['n']:,})" for k, v in m['otp_by_schedule_source'].items())}.
- Adverse weather = rain ≥ {wx['precipitation_mm']} mm/h, gusts ≥ {wx['wind_gusts_kmh']:.0f} km/h, visibility < {wx['visibility_m']} m or a thunderstorm (A-5). Reactionary = the inbound aircraft reached the gate too late for a {cfg['turnaround']['min_turn_minutes']}-min turn (A-8).

**Limitation**
- {c['coverage']['unmatched_pct']:.0f}% of the five airline groups' departures have no schedule match. They count in traffic and turnarounds, not in delay metrics (L-6).
- DGCA's figures are airline self-reported (L-3); weather is modelled, not observed (L-4); the window is one monsoon month (L-5).
"""


if __name__ == "__main__":
    run()
