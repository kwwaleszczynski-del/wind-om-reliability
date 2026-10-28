"""Logs against 10-minute SCADA: lost energy of logged stops, agreement of stop hours, clock check, code catalogue.

Uses the methods of github.com/kwwaleszczynski-del/wind-downtime-availability (``wind_downtime``, the ``scada``
extra) rather than copies: the long SCADA format and its array view, the normal-behaviour curves, the pitch /
rotor-speed stop signature and the expected power from the running neighbours.

* **Lost energy.** Each attributed downtime segment of the log is laid on the 10-minute grid. In an interval
  covered for a fraction ``c`` by logged stops, the loss is the shortfall of actual against expected power,
  capped at ``c`` times the expected energy, and shared among the segments by their overlap.
* **Agreement.** An interval counts as stopped in the log when at least half of it is covered by logged stops;
  in SCADA when the signature detector says *stop*. Precision, recall and F1 over intervals with data, and the
  stop hours per turbine and month on both sides.
* **Clock check.** The F1 of the two stop masks for the log shifted by -3 h ... +3 h. A best shift other than
  zero means the log's time zone or daylight-saving handling is off (one or two hours in Poland).
* **Code catalogue.** For logs without a stop flag or categories: the share of each code's active time during
  which SCADA shows the turbine stopped, and (with controller stop timers) which stop timer runs.

Usage:
    fleet = scada_fleet(Path("scada.parquet"), rated_kw=2050)
    sc = SCADA.build(fleet)
    seg = attribute_lost(seg, sc)
    agree = agreement(seg, sc); lag = clock_check(seg, sc)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from wind_om.events import _ns

log = logging.getLogger(__name__)
STEP_NS = pd.Timedelta("10min").value


def scada_fleet(path: Path, rated_kw=None, name: str = "farm"):
    """A long SCADA table in the ``wind_downtime`` format -> its Fleet (arrays on a 10-minute grid)."""
    from wind_downtime.schema import read_long, to_fleet

    return to_fleet(read_long(Path(path)), rated_kw=rated_kw, name=name)


@dataclass
class SCADA:
    fleet: object
    stop: np.ndarray       # (time, turbine) bool: signature says stop
    valid: np.ndarray      # (time, turbine) bool: data present
    p_hat: np.ndarray      # expected power [kW]
    shortfall: np.ndarray  # max(expected - actual, 0) x 10 min [kWh]

    @classmethod
    def build(cls, fleet, base=None) -> SCADA:
        from wind_downtime.curves import fit_curves
        from wind_downtime.detect import DETECTORS
        from wind_downtime.energy import expected_power
        from wind_downtime.schema import HOURS, STATES

        curves = fit_curves(fleet, rows=base)
        states = DETECTORS["signature"](fleet, curves)
        normal = (states == STATES.index("normal")) & (fleet.power > 0.02 * fleet.rated[None, :])
        p_hat = expected_power(fleet, curves, normal, "neighbours", base=base)
        p_hat = np.where(np.isfinite(p_hat), np.maximum(p_hat, 0.0), 0.0)
        actual = np.where(np.isfinite(fleet.power), np.maximum(fleet.power, 0.0), 0.0)
        valid = states != STATES.index("no_data")
        return cls(fleet, states == STATES.index("stop"), valid, p_hat, np.maximum(p_hat - actual, 0) * HOURS)

    @property
    def t0(self) -> int:
        return int(self.fleet.time[0].value)

    def col(self) -> dict:
        return {t: i for i, t in enumerate(self.fleet.turbines)}


def _pieces(seg: pd.DataFrame, sc: SCADA, shift_ns: int = 0):
    """(segment row, time index, turbine column, covered fraction) of every segment x interval overlap."""
    col = sc.col()
    n = len(sc.fleet.time)
    keep = seg["turbine_id"].isin(col).to_numpy()
    s, e = _ns(seg["start"]) + shift_ns - sc.t0, _ns(seg["end"]) + shift_ns - sc.t0
    rows, ks, cols, fr = [], [], [], []
    for r in np.flatnonzero(keep & (e > 0) & (s < n * STEP_NS)):
        k0, k1 = max(s[r] // STEP_NS, 0), min(-(-e[r] // STEP_NS), n)
        k = np.arange(k0, k1)
        f = (np.minimum(e[r], (k + 1) * STEP_NS) - np.maximum(s[r], k * STEP_NS)) / STEP_NS
        rows.append(np.full(len(k), r))
        ks.append(k)
        cols.append(np.full(len(k), col[seg["turbine_id"].iat[r]]))
        fr.append(f)
    if not rows:
        z = np.array([], dtype=int)
        return z, z, z, np.array([])
    return np.concatenate(rows), np.concatenate(ks), np.concatenate(cols), np.clip(np.concatenate(fr), 0, 1)


def coverage(seg: pd.DataFrame, sc: SCADA, shift_ns: int = 0) -> np.ndarray:
    """(time, turbine) fraction of each interval covered by logged stops (segments never overlap)."""
    _, k, c, f = _pieces(seg, sc, shift_ns)
    cov = np.zeros(sc.stop.shape)
    np.add.at(cov, (k, c), f)
    return np.minimum(cov, 1.0)


def attribute_lost(seg: pd.DataFrame, sc: SCADA) -> pd.DataFrame:
    """Add ``lost_kwh`` (and ``expected_kwh``) to the attributed downtime segments."""
    r, k, c, f = _pieces(seg, sc)
    cov = np.zeros(sc.stop.shape)
    np.add.at(cov, (k, c), f)
    cov = np.minimum(cov, 1.0)
    exp_kwh = sc.p_hat[k, c] / 6
    capped = np.minimum(sc.shortfall[k, c], exp_kwh * cov[k, c])
    share = np.where(cov[k, c] > 0, f / cov[k, c], 0)
    out = seg.copy()
    lost = np.zeros(len(seg))
    expd = np.zeros(len(seg))
    np.add.at(lost, r, capped * share)
    np.add.at(expd, r, exp_kwh * f)
    seen = np.zeros(len(seg), dtype=bool)
    seen[r] = True
    out["lost_kwh"] = np.where(seen, lost, np.nan)
    out["expected_kwh"] = np.where(seen, expd, np.nan)
    return out


def _f1(log_stop: np.ndarray, sc: SCADA, rows=None) -> dict:
    v = sc.valid if rows is None else sc.valid & rows[:, None]
    tp = np.sum(v & log_stop & sc.stop)
    fp = np.sum(v & ~log_stop & sc.stop)  # SCADA stop without a logged stop
    fn = np.sum(v & log_stop & ~sc.stop)  # logged stop while SCADA shows the turbine turning
    p = tp / (tp + fp) if tp + fp else np.nan
    r = tp / (tp + fn) if tp + fn else np.nan
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r > 0 else np.nan,
            "log_hours": float(np.sum(v & log_stop)) / 6, "scada_hours": float(np.sum(v & sc.stop)) / 6,
            "both_hours": float(tp) / 6}


def agreement(seg: pd.DataFrame, sc: SCADA, threshold: float = 0.5) -> tuple[dict, pd.DataFrame]:
    """Interval agreement of logged and SCADA stops, plus stop hours per turbine and month on both sides."""
    cov = coverage(seg, sc)
    log_stop = cov >= threshold
    overall = _f1(log_stop, sc)
    month = sc.fleet.time.tz_convert("UTC").tz_localize(None).to_period("M").astype(str)
    rows = []
    for m in np.unique(month):
        sel = month == m
        for i, t in enumerate(sc.fleet.turbines):
            v = sc.valid[sel, i]
            rows.append({"month": m, "turbine_id": t, "log_h": float(np.sum(cov[sel, i] * v)) / 6,
                         "scada_h": float(np.sum(sc.stop[sel, i] & v)) / 6, "valid_h": float(v.sum()) / 6})
    return overall, pd.DataFrame(rows)


def clock_check(seg: pd.DataFrame, sc: SCADA, max_shift_h: float = 3.0, step: str = "10min") -> pd.DataFrame:
    """F1 of logged against SCADA stops for the log shifted in time; the best shift should be zero."""
    out = []
    for sh in np.arange(-max_shift_h * 6, max_shift_h * 6 + 1) * pd.Timedelta(step).value:
        cov = coverage(seg, sc, int(sh))
        out.append({"shift_h": sh / 3.6e12, **_f1(cov >= 0.5, sc)})
    return pd.DataFrame(out)


def code_stop_share(ev: pd.DataFrame, sc: SCADA, timers: dict | None = None, min_intervals: int = 6) -> pd.DataFrame:
    """Per code: active intervals, share of them with SCADA showing a stop, and the dominant stop timer.

    ``timers``: {name: (time, turbine) seconds-on array}, e.g. the controller's "turbine error active" timer;
    ``<name>_share`` is the share of the code's stopped time during which that timer runs.
    """
    d = ev[ev["end"] > ev["start"]][["turbine_id", "start", "end", "code"]].reset_index(drop=True)
    d = d.assign(hours=(d["end"] - d["start"]).dt.total_seconds() / 3600, idx=np.arange(len(d)))
    r, k, c, f = _pieces(d.rename(columns={}), sc)
    sel = f >= 0.5
    r, k, c = r[sel], k[sel], c[sel]
    codes = d["code"].to_numpy()[r]
    df = pd.DataFrame({"code": codes, "stop": sc.stop[k, c], "valid": sc.valid[k, c]})
    if timers:
        for name, arr in timers.items():
            df[name] = np.nan_to_num(arr[k, c])
    df = df[df["valid"]]
    g = df.groupby("code")
    out = pd.DataFrame({"intervals": g.size(), "stop_share": g["stop"].mean()})
    if timers:
        stopped = df[df["stop"]]
        # share of the code's stopped time during which each timer runs (seconds on / 600 s per interval)
        t = (stopped.groupby("code")[list(timers)].mean() / 600).clip(upper=1)
        for name in timers:
            out[f"{name}_share"] = t[name]
        out["timer"] = t.idxmax(axis=1).where(t.max(axis=1) > 0)
    out["events"] = d.groupby("code").size()
    out["median_h"] = d.groupby("code")["hours"].median()
    return out[out["intervals"] >= min_intervals].sort_values("intervals", ascending=False).reset_index()
