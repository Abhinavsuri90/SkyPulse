"""Stage 5 - Benchmark. Check our computed OTP against DGCA's independently published OTP for BLR.

DGCA (the regulator) publishes OTP only as bar-chart labels inside a PDF. We parse those labels
into a tidy table, sanity-check the parse, and compare like with like:
same month, domestic departures, the same five airline groups, and the same 15-minute rule.
A gap is reported and explained, never forced to zero.

    python src/benchmark.py
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pdfplumber

from common import (
    DB_PATH,
    OUTPUT,
    PROCESSED,
    RAW,
    SourceError,
    atomic_write,
    get_logger,
    load_config,
)

log = get_logger("benchmark")

GROUPS = {"indigo": "IndiGo", "airindiagroup": "Air India Group", "akasaair": "Akasa Air",
          "spicejet": "SpiceJet", "allianceair": "Alliance Air"}
REASONS = ["Reactionary", "Airport", "Wx", "ATC", "Misc", "Pax", "Ramp", "Tech", "Ops"]
NUM = r"(\d{1,3}(?:\.\d)?)"
OTP_SECTION_PAGES = 5             # the OTP charts sit on the first pages of the report's 'On-Time Performance' section
IMPLIED_TAXI_SEARCH_MAX_MIN = 40  # diagnostic sweep 0..40 min: smallest taxi-out allowance that reproduces DGCA's OTP


def _norm_group(text: str) -> str | None:
    return GROUPS.get(re.sub(r"[^a-z]", "", text.lower()))


def _joined_lines(pages: list[str]) -> list[tuple[int, str]]:
    """Flatten to (page, line) and re-join labels that the PDF split from their value ('LKO' / '88.9')."""
    out: list[tuple[int, str]] = []
    for pno, text in pages:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        i = 0
        while i < len(lines):
            if re.fullmatch(r"[A-Z]{3}", lines[i]) and i + 1 < len(lines) and re.fullmatch(NUM, lines[i + 1]):
                out.append((pno, f"{lines[i]} {lines[i + 1]}"))
                i += 2
                continue
            out.append((pno, lines[i]))
            i += 1
    return out


def parse_dgca_report(pdf_path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (otp, reasons).

    otp columns: chart, airline_group, airport, otp_pct, page, dup_label
      chart = 'airline_all_airports' | 'airport_all_airlines' | 'airport_by_airline'
    reasons columns: reason, pct, page
    """
    with pdfplumber.open(pdf_path) as pdf:
        pages = [(i + 1, pg.extract_text() or "") for i, pg in enumerate(pdf.pages)]
    start = next((p for p, t in pages if "On-Time Performance" in t), None)
    if start is None:
        raise SourceError("dgca: no 'On-Time Performance' section found")
    otp_pages = [(p, t) for p, t in pages if start <= p < start + OTP_SECTION_PAGES]

    rows, reasons, section, group = [], [], None, None
    for pno, line in _joined_lines(otp_pages):
        if re.search(r"(?i)^OTP at \w+ Major Airports$", line):
            section, group = "airline_all_airports", None
            continue
        if re.search(r"(?i)^OTP of \w+ Major Airports$", line):
            section, group = "airport_all_airlines", "All 5 groups"
            continue
        if re.fullmatch(r"0 20 40 60 80 100", line):
            section = None
            continue
        if _norm_group(line) and section is None:
            section, group = "airport_by_airline", _norm_group(line)
            continue
        if section == "airline_all_airports":
            m = re.fullmatch(rf"(.+?)\s+{NUM}", line)
            if m and _norm_group(m.group(1)):
                rows.append(dict(chart=section, airline_group=_norm_group(m.group(1)), airport="ALL",
                                 otp_pct=float(m.group(2)), page=pno))
        elif section in ("airport_all_airlines", "airport_by_airline"):
            m = re.fullmatch(rf"([A-Z]{{3}})\s+{NUM}", line)
            if m:
                rows.append(dict(chart=section, airline_group=group, airport=m.group(1),
                                 otp_pct=float(m.group(2)), page=pno))
        for m in re.finditer(rf"({'|'.join(REASONS)})\s*(\d{{1,3}})%", line):
            reasons.append(dict(reason=m.group(1), pct=float(m.group(2)), page=pno))

    otp = pd.DataFrame(rows)
    if otp.empty:
        raise SourceError("dgca: OTP section found but no values parsed; chart layout changed?")
    # The official report repeats some airport labels within one chart (e.g. Alliance Air lists GAU twice).
    # Keep both, flag them, and never use a flagged value.
    otp["dup_label"] = otp.duplicated(["chart", "airline_group", "airport"], keep=False)
    reasons_df = pd.DataFrame(reasons).drop_duplicates("reason")
    return otp, reasons_df


def validate_parse(otp: pd.DataFrame, reasons: pd.DataFrame, iata: str) -> list[dict]:
    """Business sanity rules on the parsed benchmark (V-DG-*). A failed 'critical' rule stops the pipeline."""
    blr = otp[(otp.chart == "airport_all_airlines") & (otp.airport == iata)]
    checks = [
        ("V-DG-1", "critical", f"{iata} airport-level OTP present exactly once", len(blr) == 1, len(blr)),
        ("V-DG-2", "critical", "every OTP value within 0-100 %", otp.otp_pct.between(0, 100).all(),
         int((~otp.otp_pct.between(0, 100)).sum())),
        ("V-DG-3", "warn", "airline-wise overall chart has all 5 groups",
         otp[otp.chart == "airline_all_airports"].airline_group.nunique() == 5,
         otp[otp.chart == "airline_all_airports"].airline_group.nunique()),
        ("V-DG-4", "warn", "no duplicated airport labels within a chart", not otp.dup_label.any(),
         otp[otp.dup_label][["airline_group", "airport", "otp_pct"]].to_dict("records")),
        ("V-DG-5", "warn", "delay-reason shares sum to ~100 %", abs(reasons.pct.sum() - 100) <= 2,
         reasons.pct.sum()),
    ]
    return [dict(rule=r, severity=s, description=d, passed=bool(p), observed=o) for r, s, d, p, o in checks]


def load_benchmark(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    month = cfg["sources"]["dgca"]["benchmark_month"]
    pdf_path = RAW / "dgca" / f"dgca_traffic_report_{month}.pdf"
    otp, reasons = parse_dgca_report(pdf_path)
    checks = validate_parse(otp, reasons, cfg["airport"]["iata"])
    PROCESSED.mkdir(parents=True, exist_ok=True)
    otp.assign(month=month).to_csv(PROCESSED / "dgca_otp.csv", index=False)
    reasons.assign(month=month).to_csv(PROCESSED / "dgca_delay_reasons.csv", index=False)
    log.info("dgca parse: %d OTP values (%d flagged duplicate labels), %d delay reasons",
             len(otp), int(otp.dup_label.sum()), len(reasons))
    for c in checks:
        (log.info if c["passed"] else log.warning)("%s %s: %s (observed=%s)", c["rule"],
                                                   "PASS" if c["passed"] else "FAIL", c["description"], c["observed"])
    failed = [c for c in checks if c["severity"] == "critical" and not c["passed"]]
    if failed:
        raise SourceError(f"dgca: critical parse check(s) failed: {[c['rule'] for c in failed]}")
    return otp, reasons, checks


# DGCA delay-cause codes -> our attribution buckets (ours are inferred, theirs airline-coded)
REASON_BUCKET = {"Reactionary": "reactionary (inbound late)", "Wx": "weather-exposed"}
ATTRIBUTION = ("reactionary (inbound late)", "weather-exposed", "ground-side / other", "inbound not observed")


def otp_of(delay_min: pd.Series, thr: int) -> float:
    return round(100 * float((delay_min <= thr).mean()), 1)


def retime_dependency(pop: pd.DataFrame, retimed: pd.DataFrame, codes: pd.DataFrame, dgca: pd.Series, thr: int) -> dict:
    """How much the headline depends on V-DR-5: OTP with the retimed legs put back, and the evidence for each code."""
    stable = set(codes[(codes.direction == "DEP") & codes.reason.str.startswith("stable")].op_flight_icao)
    cols = ["dgca_group", "delay_min"]
    all_in = pd.concat([pop[cols], retimed[cols]])
    stable_in = pd.concat([pop[cols], retimed[retimed.op_flight_icao.isin(stable)][cols]])
    by_group = all_in.groupby("dgca_group").delay_min.apply(otp_of, thr=thr)
    both = by_group.to_frame("ours").join(dgca.rename("dgca"), how="inner")
    sampled = codes[codes.sample_offset_min.notna()]
    closer = sampled.med.abs() - sampled.sample_offset_min.abs()
    return {"legs": len(retimed), "codes_dep": int(retimed.op_flight_icao.nunique()), "stable_codes_dep": len(stable),
            "otp_all_in": otp_of(all_in.delay_min, thr), "otp_stable_in": otp_of(stable_in.delay_min, thr),
            "by_group_all_in": by_group.to_dict(),
            "same_ranking_all_in": list(both.sort_values("ours", ascending=False).index)
                                   == list(both.sort_values("dgca", ascending=False).index),
            "codes": codes.sort_values("med", ascending=False).to_dict("records"),
            "sampled": len(sampled), "sampled_closer": int((closer > thr).sum()),
            "min_closer_min": round(float(closer.min()), 1) if len(sampled) else None}


def run() -> dict:
    cfg = load_config()
    iata, thr, month = cfg["airport"]["iata"], cfg["kpi"]["on_time_threshold_min"], cfg["sources"]["dgca"]["benchmark_month"]
    otp, reasons, checks = load_benchmark(cfg)
    with sqlite3.connect(DB_PATH) as con:
        otp.assign(month=month).to_sql("dgca_otp", con, index=False, if_exists="replace")
        d = pd.read_sql("""
            SELECT l.dgca_group, l.is_domestic, l.runway_local, l.sched_local, l.delay_min, o.outcome, o.attribution
            FROM flight_leg l JOIN departure_outcome o ON o.dep_leg_id = l.leg_id
            WHERE l.direction = 'DEP' AND l.in_kpi = 1""", con)
        # Legs V-DR-5 set aside as retimes. V-DR-5 is the only rule they fail, so adding them back gives the
        # benchmark population as it would be without that rule.
        retimed = pd.read_sql("""SELECT op_flight_icao, dgca_group, delay_min FROM flight_leg
            WHERE direction = 'DEP' AND retimed = 1 AND is_domestic = 1 AND dgca_group IS NOT NULL""", con)
        observed_groups = pd.read_sql("""SELECT dgca_group, COUNT(*) n FROM flight_leg
            WHERE direction = 'DEP' AND dgca_group IS NOT NULL GROUP BY dgca_group""", con)
        spicejet_schedules = int(pd.read_sql("SELECT COUNT(*) n FROM schedule_ref WHERE op_flight_icao LIKE 'SEJ%'", con).n[0])
    # Like-for-like population: DGCA covers DOMESTIC flights of its five airline groups.
    pop = d[(d.is_domestic == 1) & d.dgca_group.notna()].copy()
    ours = round(100 * float((pop.outcome == "ON_TIME").mean()), 1)
    good = otp[~otp.dup_label]
    dgca_blr = float(good[(good.chart == "airport_all_airlines") & (good.airport == iata)].otp_pct.iloc[0])
    dg = good[(good.chart == "airport_by_airline") & (good.airport == iata)].set_index("airline_group").otp_pct
    by_group = (pop.groupby("dgca_group").outcome.agg(n="size", ours=lambda s: round(100 * (s == "ON_TIME").mean(), 1))
                .join(dg.rename("dgca"), how="outer").reset_index().rename(columns={"index": "dgca_group"}))
    by_group["gap_pts"] = (by_group.ours - by_group.dgca).round(1)
    by_group = by_group.sort_values("dgca", ascending=False).reset_index(drop=True)
    both = by_group.dropna(subset=["ours", "dgca"])
    same_order = list(both.sort_values("ours", ascending=False).dgca_group) == list(both.sort_values("dgca", ascending=False).dgca_group)

    # Which taxi-out allowance would make our OTP equal DGCA's? A diagnostic, NOT a calibration.
    raw = (pd.to_datetime(pop.runway_local) - pd.to_datetime(pop.sched_local)).dt.total_seconds() / 60
    sweep = {x: round(100 * float(((raw - x) <= thr).mean()), 1) for x in range(IMPLIED_TAXI_SEARCH_MAX_MIN + 1)}
    implied = next((x for x, v in sweep.items() if v >= dgca_blr), None)

    ours_att = pop[pop.outcome == "DELAYED"].attribution.value_counts(normalize=True).mul(100).round(1)
    theirs = reasons.set_index("reason").pct
    dgca_att = {"reactionary (inbound late)": float(theirs.get("Reactionary", 0)), "weather-exposed": float(theirs.get("Wx", 0)),
                "ground-side / other": float(theirs.drop([r for r in REASON_BUCKET if r in theirs.index]).sum())}

    codes = pd.DataFrame(json.loads((PROCESSED / "model_stats.json").read_text())["retimed"]["codes"])
    retime = retime_dependency(pop, retimed, codes, dg, thr) if len(codes) else None

    res = {"month": month, "ours_pct": ours, "n": len(pop), "dgca_blr_pct": dgca_blr, "gap_pts": round(ours - dgca_blr, 1),
           "by_group": by_group.to_dict("records"), "same_ranking": same_order, "implied_taxi_out_min": implied,
           "sweep": {k: sweep[k] for k in (0, 5, 10, 15, 20, 25, 30)}, "ours_attribution_pct": ours_att.to_dict(),
           "dgca_attribution_pct": dgca_att, "parse_checks": checks, "retime_dependency": retime,
           "observed_groups": observed_groups.set_index("dgca_group").n.to_dict(),
           "spicejet_dgca_pct": float(dg["SpiceJet"]) if "SpiceJet" in dg else None, "spicejet_schedules": spicejet_schedules,
           "dgca_airports": int(good[good.chart == "airport_all_airlines"].airport.nunique()),
           "dgca_defects": otp[otp.dup_label][["airline_group", "airport", "otp_pct"]].to_dict("records"),
           "arr_median_min": json.loads((PROCESSED / "metrics.json").read_text())["m3_arr_delay"]["median_min"]}
    dw = json.loads((PROCESSED / "metrics.json").read_text())["decision_window"]
    inside = implied is not None and dw["ranking_stable_from"] <= implied <= (dw["ground_leads_up_to"] or 999)
    res["decision_note"] = (f"That allowance is {'inside' if inside else 'OUTSIDE'} the range where the recommendation holds "
                            f"({dw['ranking_stable_from']}–{dw['ground_leads_up_to']} min, see the evidence table), so "
                            f"{'the benchmark-consistent reading supports the same decision.' if inside else 'the decision should be revisited.'}")
    atomic_write(PROCESSED / "benchmark.json", json.dumps(res, indent=2, default=str))
    atomic_write(OUTPUT / "benchmark_comparison.md", render(res, cfg, otp))
    log.info("benchmark: ours %.1f%% (n=%d) vs DGCA %.1f%% -> gap %+.1f pts; same airline ranking=%s; implied taxi-out %s min",
             ours, len(pop), dgca_blr, res["gap_pts"], same_order, implied)
    return res


def retime_section(r: dict, cfg: dict) -> tuple[str, str]:
    """Headline row + section showing how much the agreement depends on V-DR-5, with the evidence per flight code."""
    t, dgca = r["retime_dependency"], r["dgca_blr_pct"]
    thr, rt = cfg["kpi"]["on_time_threshold_min"], cfg["kpi"]["retime"]
    if not t:
        return "", ""
    row = (f"| SkyPulse with the {t['legs']:,} retimed departures left in (see *What the headline depends on*) | "
           f"{t['otp_all_in']:.1f}% (gap {t['otp_all_in'] - dgca:+.1f} points) |\n")
    dg = {g["dgca_group"]: g["dgca"] for g in r["by_group"]}
    groups = ", ".join(f"{g} {v:.1f}% (DGCA {dg[g]:.1f}%)" for g, v in t["by_group_all_in"].items() if g in dg)
    codes = "\n".join(
        f"| {c['op_flight_icao']} | {c['direction']} | {c['n']:.0f} | {c['med']:+.0f} | {c['iqr']:.0f} | "
        f"{'no actuals' if pd.isna(c['sample_offset_min']) else format(c['sample_offset_min'], '+.0f')} | {c['reason']} |"
        for c in t["codes"])
    unsampled = len(t["codes"]) - t["sampled"]
    evidence = (f"For {t['sampled_closer']} of the {t['sampled']} codes that also have September actuals, the September flight sits "
                f"more than {thr} min closer to its schedule than the typical August flight did (smallest difference: "
                f"{t['min_closer_min']:.0f} min). The timetable moved, not the flight."
                if t["sampled"] else "")
    if unsampled:
        evidence += f" {unsampled} code(s) have no September actuals and were flagged on their August offset alone."
    section = f"""## What the headline depends on: retimed flights (V-DR-5)

The close agreement depends on setting aside {t['legs']:,} departures on {t['codes_dep']} flight codes as **retimes**. These are flights whose schedule changed between August and the September sample that our schedules come from. Measured against September times, a timetable change looks like a delay on every operation. This is the biggest judgment in the benchmark, so here is its effect:

| Benchmark population | SkyPulse OTP | Gap to DGCA |
|---|---|---|
| As published: retimes excluded | {r['ours_pct']:.1f}% | {r['gap_pts']:+.1f} points |
| Only the {t['stable_codes_dep']} stable-offset retimes left in (the least certain ones) | {t['otp_stable_in']:.1f}% | {t['otp_stable_in'] - dgca:+.1f} points |
| All retimes left in | {t['otp_all_in']:.1f}% | {t['otp_all_in'] - dgca:+.1f} points |

With every retime left in: {groups}. The airline ranking is **{'still identical' if t['same_ranking_all_in'] else 'different'}**, so {'the ranking does not depend on this rule; the overall level does' if t['same_ranking_all_in'] else 'both the ranking and the level depend on this rule'}.

**Why they are retimes, not delays.** Real delay varies from day to day. Each of these codes was off its September schedule on at least {rt['share']:.0%} of its (at least {rt['min_ops']}) August operations: late by more than {rt['median_late_min']} min at the median, early by more than {-rt['median_early_min']} min, or late by a steady amount (IQR ≤ {rt['stable_iqr_max']} min) while the September sample shows the same flight on time. {evidence}

| Flight | Direction | August ops | August median offset (min) | Spread, IQR (min) | September sample offset (min) | Flagged as |
|---|---|---|---|---|---|---|
{codes}

The thresholds live in `config.yaml` (`kpi.retime`). Anyone who disagrees with a code can change them and rerun.
"""
    return row, section


def render(r: dict, cfg: dict, otp: pd.DataFrame) -> str:
    taxi, iata = cfg["taxi"]["default_taxi_out_min"], cfg["airport"]["iata"]

    def cell(v, fmt, blank=""):
        return blank if pd.isna(v) else format(v, fmt)

    rows = "\n".join(f"| {g['dgca_group']} | {cell(g['n'], ',.0f')} | {cell(g['ours'], '.1f', 'not observed')}% | "
                     f"{cell(g['dgca'], '.1f')}% | {cell(g['gap_pts'], '+.1f')} |" for g in r["by_group"]).replace("not observed%", "not observed")
    sweep = " | ".join(f"{k} min → {v:.1f}%" for k, v in r["sweep"].items())
    att = "\n".join(f"| {k} | {r['ours_attribution_pct'].get(k, 0):.0f}% | "
                    f"{cell(r['dgca_attribution_pct'].get(k), '.0f', 'no such code')}{'%' if k in r['dgca_attribution_pct'] else ''} |"
                    for k in ATTRIBUTION)
    checks = "\n".join(f"| {c['rule']} | {'PASS' if c['passed'] else 'FLAG'} | {c['description']} | {c['observed']} |" for c in r["parse_checks"])
    gap = r["gap_pts"]
    verdict = (f"**agree closely: {abs(gap):.1f} points apart**" if abs(gap) <= 2 else
               f"**differ by {abs(gap):.1f} points** (ours {'lower' if gap < 0 else 'higher'})")
    retime_row, retime_txt = retime_section(r, cfg)
    t = r["retime_dependency"]
    widens = t and abs(t["otp_all_in"] - r["dgca_blr_pct"]) > abs(gap)
    population_note = (f" Left in, they would widen the gap to {t['otp_all_in'] - r['dgca_blr_pct']:+.1f} points; "
                       "see *What the headline depends on* below." if widens else "")

    sj = r["observed_groups"].get("SpiceJet", 0)
    big2 = [g for g, _ in sorted(r["observed_groups"].items(), key=lambda kv: -kv[1])[:2]]  # the two largest airline groups
    sj_dgca = f"at {r['spicejet_dgca_pct']:.1f}% OTP" if r["spicejet_dgca_pct"] is not None else "with no BLR value"
    sj_sched = ("had no SpiceJet schedules" if not r["spicejet_schedules"] else
                f"had only {r['spicejet_schedules']} SpiceJet schedule(s)")

    defects = pd.DataFrame(r["dgca_defects"])
    if defects.empty:
        defect_txt = "V-DG-4 found no duplicated labels in DGCA's charts."
    else:
        parts = [f"the {g} chart lists {a} {'twice' if len(x) == 2 else f'{len(x)} times'} "
                 f"({' and '.join(f'{v:.1f}' for v in x.otp_pct)})" for (g, a), x in defects.groupby(["airline_group", "airport"])]
        defect_txt = (f"V-DG-4 found a **defect in DGCA's own report**: {'; '.join(parts)}. These values are flagged and none is used. "
                      f"{iata + ' is unaffected. ' if iata not in set(defects.airport) else ''}"
                      "An authoritative source is still a source to check, not ground truth.")
    return f"""# Benchmark: SkyPulse OTP vs DGCA's published OTP for BLR ({r['month']})

_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `src/benchmark.py`._

**Why this exists:** a pipeline that only checks its own maths is marking its own homework. DGCA, India's aviation regulator, publishes an OTP for BLR every month. We never use it to compute anything. It is only the independent number we test ourselves against.

## Headline

| | OTP, BLR domestic departures, {r['month']} |
|---|---|
| **DGCA published** (airline-reported, 5 airline groups) | **{r['dgca_blr_pct']:.1f}%** |
| **SkyPulse computed** (OpenSky-observed, same 5 groups, domestic only, n = {r['n']:,}) | **{r['ours_pct']:.1f}%** |
| Gap | {gap:+.1f} points |
{retime_row}
Same month, same airport, same airline groups, domestic flights only, same rule (delayed = more than 15 min after scheduled departure). The two numbers {verdict}.

## By airline group at BLR

| Airline group | SkyPulse departures | SkyPulse OTP | DGCA OTP | Gap (pts) |
|---|---|---|---|---|
{rows}

**Airline ranking: {'identical' if r['same_ranking'] else 'different'}** between SkyPulse and DGCA. {'Two independent methods (radar-observed times vs airline self-reporting) put the airlines in the same order. That is strong evidence that the pipeline measures the right thing, even where the level differs.' if r['same_ranking'] else 'The order differs; see the explanations below.'}

## Why the numbers differ (plain English)

1. **Gate time vs runway time (the biggest unknown).** DGCA counts a flight as departed when the airline reports it left the gate (off-block). OpenSky sees the aircraft only once it is airborne. We subtract a {taxi}-minute taxi allowance (assumption A-2), which no source available to us can measure (K-8). OTP is very sensitive to that allowance: {sweep}. Our number would equal DGCA's at a taxi-out allowance of **{r['implied_taxi_out_min']} minutes**. If BLR's real average taxi-out is around that, the taxi assumption alone explains the whole gap. That average is exactly what public data cannot tell us (U-1). {r['decision_note']} We report this as a diagnostic and **deliberately do not tune the allowance to hit DGCA's figure**; doing that would turn the benchmark into an input.
2. **Self-reported vs observed.** DGCA's figure is compiled from what each airline reports, and the report itself says so on every page. Ours is observed independently. Neither is audited against the other, and some difference is expected.
3. **Population.** We can measure only departures we could match to a real schedule: {r['n']:,} domestic departures, about three quarters of the 5 groups' BLR flights (L-6). We also exclude flight codes whose schedule changed after August (V-DR-5).{population_note} DGCA covers every flight the airlines operated.
4. **SpiceJet.** DGCA lists SpiceJet at BLR {sj_dgca}. OpenSky saw only {sj} SpiceJet departures from BLR in all of August ({100 * sj / max(1, sum(r['observed_groups'].values())):.1f}% of the 5 groups' departures). The September AviationStack sample {sj_sched}, so SpiceJet is outside our population. At that volume it barely moves DGCA's BLR figure.
5. **Schedule vintage.** Our schedules come from a September sample applied to August (A-3). Flights that were retimed are caught by V-DR-5; smaller retimes may remain and would bias individual flights either way.

{retime_txt}
## Delay causes: our inference vs DGCA's national split

| Bucket | SkyPulse ({iata}, inferred) | DGCA (all {r['dgca_airports']} airports, airline-coded) |
|---|---|---|
{att}

DGCA's "Reactionary" covers any delay the airline attributes to the previous rotation. Ours is stricter: the inbound aircraft must have reached the gate too late to be turned in {cfg['turnaround']['min_turn_minutes']} minutes. We count a delay as ground-side only when the aircraft was provably at BLR in time; delays whose inbound aircraft OpenSky never saw are kept apart as "inbound not observed". So our reactionary share is expected to be lower, and our ground-side share higher. Both sources agree that **weather is a small share of delay**.

**This is the one place the benchmark challenges our conclusion, and we say so.** DGCA's split is national: all {r['dgca_airports']} airports, including congested Delhi and Mumbai, where knock-on delay is endemic. The BLR-specific evidence points the other way: inbound flights reached the gate a median {abs(r['arr_median_min']):.0f} min early in August. We keep the ground-side recommendation, and label it with this caveat in the evidence table. The next step for the ops manager is to ask {big2[0]} and {big2[1]} for their **{iata}-only** delay codes for August; that single request would confirm or overturn it.

## Checks on the benchmark itself

The benchmark is parsed from chart labels in a PDF, so the parse is validated too:

| Rule | Result | Check | Observed |
|---|---|---|---|
{checks}

{defect_txt}
"""


if __name__ == "__main__":
    run()
