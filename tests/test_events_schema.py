"""Hard cases: overlapping events, open events, month boundaries, DST, fleet-wide events, cascades, resets."""
import io

import numpy as np
import pandas as pd
import pytest

from wind_om import compound, events
from wind_om.analysis import analyse
from wind_om.mapping import apply_mapping
from wind_om.pareto import pareto
from wind_om.schema import normalise, read_log, to_utc, validate, write_log
from wind_om.synthetic import mapping_frame, synthetic_log


def T(s):
    return pd.Timestamp(s, tz="UTC")


def make(rows):
    df = pd.DataFrame(rows, columns=["turbine_id", "start", "end", "code", "category", "stop"])
    df["start"], df["end"] = pd.to_datetime(df["start"], utc=True), pd.to_datetime(df["end"], utc=True)
    df["description"] = ""
    return apply_mapping(validate(df), None)


def test_overlaps_attributed_once_to_first_up():
    ev = make([
        ("A", "2024-01-01 10:00", "2024-01-01 14:00", "fault1", "forced_outage", True),
        ("A", "2024-01-01 11:00", "2024-01-01 12:00", "nested", "forced_outage", True),   # inside the first
        ("A", "2024-01-01 13:00", "2024-01-01 18:00", "service", "scheduled_maintenance", True),  # tail
        ("A", "2024-01-01 18:05", "2024-01-01 18:30", "fault1", "forced_outage", True),   # restart failed
        ("A", "2024-01-02 09:00", "2024-01-02 10:00", "fault2", "forced_outage", True),   # new episode
    ])
    ep, seg = events.episodes(events.close_events(ev))
    hours = seg.groupby("idx")["hours"].sum().reindex(ev.index, fill_value=0)
    assert hours.tolist() == pytest.approx([4.0, 0.0, 4.0, 25 / 60, 1.0])
    assert seg["hours"].sum() == pytest.approx(4 + 4 + 25 / 60 + 1)  # the union, nothing twice
    assert len(ep) == 2
    first = ep.iloc[0]
    assert first["duration_h"] == pytest.approx(8.5) and first["stop_h"] == pytest.approx(8 + 25 / 60)
    # a fault was the reason at some moment: failure episode, caused by the fault, not by the service stop
    assert bool(first["failure"]) and first["code"] == "fault1" and first["n_events"] == 4


def test_open_stop_closed_at_next_stop():
    ev = make([
        ("A", "2024-01-01 10:00", None, "fault1", "forced_outage", True),
        ("A", "2024-01-01 15:00", "2024-01-01 16:00", "fault2", "forced_outage", True),
        ("A", "2024-01-01 15:30", None, "info", "information", False),
    ])
    out = events.close_events(ev, window_end=T("2024-01-02"))
    assert out.loc[0, "end"] == T("2024-01-01 15:00") and out.loc[0, "end_imputed"]
    assert out.loc[2, "end"] == out.loc[2, "start"]


def test_month_boundary_split_in_local_time():
    # 3 h stop from 23:00 to 02:00 local time across 31 March / 1 April (Europe/Warsaw, summer time: UTC+2)
    ev = make([("A", "2026-03-31 21:00", "2026-04-01 00:00", "f", "forced_outage", True)])
    ev = events.close_events(ev)
    ep, seg = events.episodes(ev)
    per = events.periods(T("2026-03-01"), T("2026-04-30"), tz="Europe/Warsaw").set_index("period")
    assert per.loc["2026-03", "hours"] == 743 and per.loc["2026-04", "hours"] == 720
    march = pareto(ev, seg, ep, start=per.loc["2026-03", "start"], end=per.loc["2026-03", "end"])
    april = pareto(ev, seg, ep, start=per.loc["2026-04", "start"], end=per.loc["2026-04", "end"])
    assert march["hours"].sum() == pytest.approx(1.0) and april["hours"].sum() == pytest.approx(2.0)
    # the failure counts in the month it started
    assert march["failures"].sum() == 1 and april["failures"].sum() == 0
    oct_ = events.periods(T("2026-10-01"), T("2026-10-15"), tz="Europe/Warsaw")
    assert oct_["hours"].iloc[0] == 745


def _local_csv(ev, tz):
    buf = io.StringIO()
    out = ev.copy()
    for c in ("start", "end"):
        out[c] = out[c].dt.tz_convert(tz).dt.strftime("%d.%m.%Y %H:%M:%S")
    out.to_csv(buf, index=False, sep=";")
    return buf.getvalue()


def test_dst_round_trip_europe_warsaw():
    # events every 7 minutes on two turbines through both 2026 clock changes, listed per turbine in time order
    rows = []
    for tid in ("WTG01", "WTG02"):
        for day in ("2026-03-29 00:00", "2026-10-25 00:00"):
            t = pd.date_range(T(day) - pd.Timedelta(hours=1), periods=40, freq="7min")
            for k, s in enumerate(t):
                rows.append((tid, s, s + pd.Timedelta(minutes=5 + 20 * (k % 3)), str(100 + k % 4), "x", "Fault"))
    utc = pd.DataFrame(rows, columns=["turbine_id", "start", "end", "code", "description", "category"])
    text = _local_csv(utc, "Europe/Warsaw")
    cfg = {"read": {"sep": ";"}, "timezone": "Europe/Warsaw", "ambiguous": "infer",
           "datetime_format": "%d.%m.%Y %H:%M:%S", "category_map": {"Fault": "forced_outage"}, "stop": "all"}
    back = normalise(pd.read_csv(io.StringIO(text), sep=";", dtype=str), cfg)
    exp = utc.sort_values(["turbine_id", "start"]).reset_index(drop=True)
    assert (back["start"].to_numpy() == exp["start"].to_numpy()).all()
    assert (back["end"].to_numpy() == exp["end"].to_numpy()).all()
    # the same file read as if it were UTC is off by one or two hours
    naive = normalise(pd.read_csv(io.StringIO(text), sep=";", dtype=str), {**cfg, "timezone": "UTC"})
    assert ((naive["start"] - exp["start"]).dt.total_seconds().abs() >= 3600).all()


def test_to_utc_offsets_and_gap():
    s = pd.Series(["2026-10-25 02:30:00+02:00", "2026-10-25 02:30:00+01:00"])
    assert to_utc(s, "Europe/Warsaw").tolist() == [T("2026-10-25 00:30"), T("2026-10-25 01:30")]
    gap = to_utc(pd.Series(["2026-03-29 02:30:00"]), "Europe/Warsaw")  # does not exist: shifted to 03:00
    assert gap.iloc[0] == T("2026-03-29 01:00")
    with pytest.raises(ValueError):
        to_utc(pd.Series(["2026-10-25 02:30:00"]), "Europe/Warsaw", ambiguous="raise")


def test_read_log_with_yaml_and_duration(tmp_path):
    csv = tmp_path / "alarms.csv"
    csv.write_text("WTG;Begin;Dur;Code;Text;Type\n"
                   "T1;01.07.2026 10:00:00;3600;55;Pitch fault;Fault\n"
                   "T1;01.07.2026 12:00:00;600;9;Info only;Info\n", encoding="utf-8")
    cfg = tmp_path / "oem.yaml"
    cfg.write_text("read: {sep: ';'}\ncolumns: {turbine_id: WTG, start: Begin, code: Code, description: Text,"
                   " category: Type}\nduration: {column: Dur, unit: s}\ntimezone: Europe/Warsaw\n"
                   "datetime_format: '%d.%m.%Y %H:%M:%S'\nstop: {column: Type, values: [Fault]}\n"
                   "category_map: {Fault: forced_outage, Info: information}\n", encoding="utf-8")
    log = read_log(csv, cfg)
    assert log.loc[0, "start"] == T("2026-07-01 08:00") and log.loc[0, "end"] == T("2026-07-01 09:00")
    assert log["stop"].tolist() == [True, False]
    assert log["category"].tolist() == ["forced_outage", "information"]
    p = write_log(log, tmp_path / "out.csv", tz="Europe/Warsaw")
    assert "2026-07-01 10:00:00" in p.read_text()


def test_fleet_wide_grid_loss_not_a_turbine_failure():
    log, _ = synthetic_log(n_turbines=10, years=1, process="hpp", mtbf_h=500, grid_losses=4, seed=2)
    a = analyse(log, mapping_frame())
    fw = a.ep[a.ep["fleet_wide"]]
    assert fw["fleet_group"].nunique() == 4 and len(fw) >= 40
    assert not a.fail["code"].eq("3500").any()


def test_cascades_and_restoration():
    log, truth = synthetic_log(n_turbines=10, years=2, process="hpp", mtbf_h=300, ttr_median_h=12, ttr_sigma=0.5,
                               cascade_prob=1.0, onsite_prob=1.0, response_h=6.0, seed=6)
    a = analyse(log, mapping_frame())
    pairs = a.pairs.set_index(["primary", "secondary"])
    assert pairs.loc[("1001", "7101"), "confidence"] == pytest.approx(1.0, abs=0.02)
    assert pairs.loc[("2002", "7202"), "lift"] > 3
    res = compound.restoration_summary(a.res).set_index("restoration")
    on = a.res[a.res["restoration"] == "on_site"]
    assert on["response_h"].median() == pytest.approx(truth.response_h, abs=0.01)
    assert res.loc["on_site", "share"] > 0.6
    # the manual stop that follows each fault does not hide the failure behind it
    assert len(a.fail) == len(a.res) and a.fail["category"].eq("forced_outage").all()


def test_exposure_and_interval_helpers():
    s, e = events.union(np.array([0, 5, 20]), np.array([10, 8, 30]))
    assert s.tolist() == [0, 20] and e.tolist() == [10, 30]
    s, e = events.subtract(np.array([0]), np.array([100]), np.array([10, 50]), np.array([20, 60]))
    assert list(zip(s, e)) == [(0, 10), (20, 50), (60, 100)]


def test_silence_is_not_observation():
    # a turbine that logs routine messages every day, then nothing for 40 days (missing files)
    days = pd.date_range("2024-01-01", "2024-06-30", freq="D", tz="UTC")
    days = days[(days < T("2024-03-01")) | (days >= T("2024-04-10"))]
    ev = make([("A", d, d + pd.Timedelta("1min"), "0", "information", False) for d in days])
    full = events.exposure(ev, T("2024-01-01"), T("2024-07-01"))
    cut = events.exposure(ev, T("2024-01-01"), T("2024-07-01"), max_silence="7D")
    assert full["hours"].sum() == pytest.approx(182 * 24)
    assert cut["hours"].sum() == pytest.approx(182 * 24 - (40 * 24 - 1 / 60) - (24 - 1 / 60), abs=0.1)
    assert len(cut) == 2
