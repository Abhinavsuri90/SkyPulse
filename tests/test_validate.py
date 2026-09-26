"""Tests for the validation rules and the dependability guarantees they rely on.

    pytest -q
"""
import json
import sys

import pandas as pd
import pytest
import requests

import common
import ingest
import model
import pipeline
import validate
from benchmark import parse_dgca_report, validate_parse
from common import RAW, SourceError, load_config

CFG = load_config()


def leg(**kw):
    base = dict(leg_id="DEP-800001-1000", direction="DEP", icao24="800001", callsign="IGO2145", first_seen=1000,
                last_seen=6000, est_dep_airport="VOBL", est_arr_airport="VIDP", dep_horiz_m=4000, dep_vert_m=300,
                arr_horiz_m=None, arr_vert_m=None, local_day="2026-08-01", is_crosscheck=0, source_file="x")
    base.update(kw)
    return base


def rules_hit(frame, fn, *args):
    flags = validate.Flags()
    fn(frame, *args, flags)
    return flags.frame()


# ---------------------------------------------------------------- OpenSky legs
def test_clean_leg_raises_no_exclusion():
    fl = rules_hit(pd.DataFrame([leg()]), validate.validate_opensky, CFG)
    assert fl[fl.severity == "exclude"].empty


@pytest.mark.parametrize("bad, rule", [
    (dict(icao24="80000G"), "V-OS-1"),                     # not hex
    (dict(last_seen=900), "V-OS-2"),                        # lands before it takes off
    (dict(callsign=""), "V-OS-4"),                          # no callsign = not a scheduled flight
    (dict(callsign="VTNAB"), "V-OS-4"),                     # registration-style callsign
    (dict(dep_horiz_m=25000), "V-OS-6"),                    # first seen 25 km out: take-off time unreliable
])
def test_bad_leg_is_flagged_not_dropped(bad, rule):
    frame = pd.DataFrame([leg(**bad)])
    fl = rules_hit(frame, validate.validate_opensky, CFG)
    assert rule in set(fl.rule)
    assert len(frame) == 1  # flagged, never deleted


def test_duplicate_leg_flags_only_the_copy():
    frame = pd.DataFrame([leg(), leg(leg_id="DEP-800001-1000#2")])
    fl = rules_hit(frame, validate.validate_opensky, CFG)
    assert list(fl[fl.rule == "V-OS-3"].entity_id) == ["DEP-800001-1000#2"]


def test_merged_round_trip_excludes_only_the_inbound_end():
    # BLR -> BLR over 5 h: OpenSky missed the outstation stop. The take-off is real; the landing carries the wrong callsign.
    rt = dict(est_dep_airport="VOBL", est_arr_airport="VOBL", first_seen=0, last_seen=5 * 3600)
    frame = pd.DataFrame([leg(leg_id="DEP-a", **rt), leg(leg_id="ARR-a", direction="ARR", arr_horiz_m=8000, arr_vert_m=300, **rt)])
    fl = rules_hit(frame, validate.validate_opensky, CFG)
    assert set(fl[fl.rule == "V-OS-7"].entity_id) == {"ARR-a"}


def test_short_circuit_excludes_both_ends():
    c = dict(est_dep_airport="VOBL", est_arr_airport="VOBL", first_seen=0, last_seen=20 * 60)
    frame = pd.DataFrame([leg(leg_id="DEP-c", **c), leg(leg_id="ARR-c", direction="ARR", arr_horiz_m=8000, arr_vert_m=300, **c)])
    fl = rules_hit(frame, validate.validate_opensky, CFG)
    assert set(fl[fl.rule == "V-OS-7"].entity_id) == {"DEP-c", "ARR-c"}


# ---------------------------------------------------------------- AviationStack
def test_codeshare_and_uppercase_icao24_rules():
    a = pd.DataFrame([
        dict(as_id="1", direction="DEP", flight_date="2026-09-22", op_flight_icao="AIC2818", is_codeshare=0,
             icao24_raw="8017F2", icao24="8017f2", raw_scheduled="2026-09-22T18:00:00+00:00", sched_local="2026-09-22T18:00:00", status="landed"),
        dict(as_id="2", direction="DEP", flight_date="2026-09-22", op_flight_icao="AIC2818", is_codeshare=1,
             icao24_raw="8017F2", icao24="8017f2", raw_scheduled="2026-09-22T18:00:00+00:00", sched_local="2026-09-22T18:00:00", status="landed"),
    ])
    fl = rules_hit(a, validate.validate_aviationstack)
    assert set(fl[fl.rule == "V-AS-1"].entity_id) == {"2"}          # codeshare duplicate excluded
    assert set(fl[fl.rule == "V-AS-3"].entity_id) == {"1", "2"}     # case normalised (and recorded)


def test_mislabelled_utc_is_read_as_local_clock_time():
    assert validate._local_naive("2026-09-22T18:00:00+00:00") == "2026-09-22T18:00:00"


# ---------------------------------------------------------------- weather
def test_missing_weather_hours_are_critical():
    w = pd.DataFrame({"hour_local": ["2026-08-01T00:00"], "precipitation": [0.0], "wind_gusts_10m": [10.0],
                      "visibility": [20000.0], "weather_code": [3]})
    fl = rules_hit(w, validate.validate_weather, CFG)
    assert "V-WX-1" in set(fl.rule) and validate.RULES["V-WX-1"][0] == "critical"


# ---------------------------------------------------------------- derived rules
def test_schedule_wraps_midnight_to_the_nearest_day():
    observed = pd.Series(pd.to_datetime(["2026-08-02 00:20"]))
    sched = model.resolve_schedule(observed, pd.Series(["23:50"]))
    assert sched.iloc[0] == pd.Timestamp("2026-08-01 23:50")  # 30 min late, not 23.5 h early


def test_retime_is_detected_but_a_noisy_late_flight_is_not():
    rt = CFG["kpi"]["retime"]
    retimed = [290 + i % 7 for i in range(10)]            # +290 min every day, tiny spread: schedule changed
    noisy = [5, 40, -3, 70, 12, 95, -8, 30, 2, 55]         # genuinely delay-prone flight
    legs = pd.DataFrame({"direction": "DEP", "op_flight_icao": ["AXB2001"] * 10 + ["IGO123"] * 10,
                         "delay_min": retimed + noisy})
    found = model.detect_retimes(legs, CFG["kpi"]["on_time_threshold_min"], rt)
    assert list(found.op_flight_icao) == ["AXB2001"]


def test_small_retime_needs_the_schedule_sample_to_contradict_it():
    # +40 min every day with a tight spread. On its own that could be a chronic delay, so it is kept...
    rt, thr = CFG["kpi"]["retime"], CFG["kpi"]["on_time_threshold_min"]
    legs = pd.DataFrame({"direction": "DEP", "op_flight_icao": "AIC2511", "delay_min": [38, 40, 42, 39, 44, 41, 40, 43]})
    assert model.detect_retimes(legs, thr, rt).empty
    # ...but if the September sample shows the same flight leaving on time, the August schedule was different.
    on_time = pd.DataFrame({"direction": ["DEP"], "op_flight_icao": ["AIC2511"], "sample_offset_min": [-4.0]})
    found = model.detect_retimes(legs, thr, rt, on_time)
    assert list(found.op_flight_icao) == ["AIC2511"] and found.reason.iloc[0].startswith("stable offset")
    # A sample showing it late too is consistent with a real chronic delay: not a retime.
    late_too = on_time.assign(sample_offset_min=45.0)
    assert model.detect_retimes(legs, thr, rt, late_too).empty


def test_delay_is_ground_side_only_when_the_aircraft_was_provably_at_blr():
    std = pd.Timestamp("2026-08-10 10:00")  # every departure: STD 10:00, left 40 min late, clear weather
    L = pd.DataFrame({"leg_id": ["D-ok", "D-late", "D-parked", "D-unseen", "A-ok", "A-late", "A-parked"],
                      "direction": ["DEP"] * 4 + ["ARR"] * 3, "in_kpi": 1, "delay_min": 40.0,
                      "sched_local": std, "gate_local_est": std + pd.Timedelta(minutes=40)})
    ta = pd.DataFrame({"dep_leg_id": ["D-ok", "D-late", "D-parked"], "arr_leg_id": ["A-ok", "A-late", "A-parked"],
                       "wheels_down_local": [std - pd.Timedelta(hours=2), std - pd.Timedelta(minutes=5), std - pd.Timedelta(hours=15)],
                       "ground_min": [160.0, 45.0, 940.0], "valid": [1, 1, 0]})
    W = pd.DataFrame({"hour_local": [std], "adverse": [0]})
    got = model.build_departure_outcomes(L, ta, W, CFG).set_index("dep_leg_id").attribution.to_dict()
    assert got == {"D-ok": "ground-side / other",              # landed 2 h before STD
                   "D-late": "reactionary (inbound late)",     # landed 5 min before STD: no 30-min turn possible
                   "D-parked": "ground-side / other",          # parked overnight: was at BLR in time
                   "D-unseen": "inbound not observed"}         # OpenSky never saw it land: not blamed on the ground


# ---------------------------------------------------------------- benchmark source
def test_dgca_parser_reads_blr_and_flags_the_reports_own_duplicate():
    otp, reasons = parse_dgca_report(RAW / "dgca" / "dgca_traffic_report_2026-08.pdf")
    blr = otp[(otp.chart == "airport_all_airlines") & (otp.airport == "BLR")]
    assert blr.otp_pct.tolist() == [92.0]
    dup = otp[otp.dup_label]
    assert set(dup.airport) == {"GAU"} and set(dup.airline_group) == {"Alliance Air"}
    checks = {c["rule"]: c["passed"] for c in validate_parse(otp, reasons, "BLR")}
    assert checks["V-DG-1"] and checks["V-DG-2"] and not checks["V-DG-4"]


@pytest.mark.parametrize("name", ["traffic Data August 26.pdf", "TrafficDataAug26.pdf", "TrafficReportAugust2026.pdf"])
def test_dgca_month_pattern_tolerates_inconsistent_file_names(name):
    assert ingest.dgca_month_pattern("2026-08").search(name)


# ---------------------------------------------------------------- dependability
def test_external_call_retries_then_fails_loudly(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(common.requests, "request", boom)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    monkeypatch.delenv("SKYPULSE_OFFLINE", raising=False)
    with pytest.raises(SourceError, match="failed after 3 attempts"):
        common.http_request("GET", "https://example.invalid", log=common.get_logger("test"), label="t", cfg=CFG)
    assert len(calls) == CFG["http"]["retries"]


def test_offline_mode_refuses_network(monkeypatch):
    monkeypatch.setenv("SKYPULSE_OFFLINE", "1")
    with pytest.raises(SourceError, match="offline"):
        common.http_request("GET", "https://example.invalid", log=common.get_logger("test"), label="t", cfg=CFG)


def test_aviationstack_hard_cap_blocks_any_call(monkeypatch, tmp_path):
    ledger = tmp_path / "_call_ledger.json"
    ledger.write_text(json.dumps({"cap": 30, "calls": [{"n": i} for i in range(30)]}))
    monkeypatch.setattr(ingest, "LEDGER", ledger)
    monkeypatch.setenv("AVIATIONSTACK_ACCESS_KEY", "test-key-not-real")
    monkeypatch.setattr(ingest, "http_request", lambda *a, **k: pytest.fail("a call was made past the cap"))
    out = ingest.sample_aviationstack(CFG, n_calls=5)
    assert out["status"] == "cap_reached"


def test_secrets_never_reach_error_messages(monkeypatch):
    key = "secret-key-123"

    def boom(method, url, params=None, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: /v1/flights?access_key={params['access_key']}")

    monkeypatch.setattr(common.requests, "request", boom)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    monkeypatch.delenv("SKYPULSE_OFFLINE", raising=False)
    with pytest.raises(SourceError) as e:
        common.http_request("GET", "https://example.invalid", params={"access_key": key}, log=common.get_logger("test"),
                            label="t", cfg=CFG)
    assert key not in str(e.value) and "***" in str(e.value)


def test_failed_late_stage_restores_every_output(monkeypatch, tmp_path):
    out, proc = tmp_path / "output", tmp_path / "processed"
    out.mkdir()
    proc.mkdir()
    (out / "evidence_table.md").write_text("last good run")
    (proc / "metrics.json").write_text('{"otp": 91.6}')
    monkeypatch.setattr(pipeline, "GUARDED", [out, proc])
    monkeypatch.setattr(pipeline, "OUTPUT", out)
    monkeypatch.setattr(pipeline, "FAIL_MARKER", out / "LAST_RUN_FAILED.md")
    monkeypatch.setenv("SKYPULSE_FAULT", "")  # restored after the test; main() overwrites it

    def metrics_stage():  # the metrics stage rewrites outputs, then the benchmark stage fails
        (out / "evidence_table.md").write_text("half of a new run")
        (proc / "metrics.json").unlink()
        (proc / "stray.csv").write_text("x")

    for mod, fn in ((pipeline.ingest, lambda **k: []), (pipeline.validate, dict), (pipeline.model, dict),
                    (pipeline.metrics, metrics_stage)):
        monkeypatch.setattr(mod, "run", fn)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "--fault", "benchmark"])

    assert pipeline.main() == 1
    assert (out / "evidence_table.md").read_text() == "last good run"
    assert (proc / "metrics.json").read_text() == '{"otp": 91.6}'
    assert not (proc / "stray.csv").exists()
    status = json.loads((out / "run_status.json").read_text())
    assert status["failed_stage"] == "benchmark" and [s["stage"] for s in status["completed_stages"]][-1] == "metrics"
    assert (out / "LAST_RUN_FAILED.md").exists()
