"""Synthetic status logs with known reliability parameters, for tests and for trying the tool without data.

Failures are drawn on each turbine's *operating* clock (time not stopped), so the true MTBF is exactly the
mean operating time between failures. Three processes:

``hpp``         constant rate, MTBF ``mtbf_h``
``weibull``     renewal process, times between failures Weibull(``shape``, ``scale_h``)
``power_law``   Crow-AMSAA NHPP with intensity ``lam * beta * age**(beta - 1)`` in turbine age (hours since
                ``origin``); downtime is kept short so operating and calendar age stay close

Restore times are lognormal (median ``ttr_median_h``, log-standard deviation ``ttr_sigma``). Each failure gets
a code drawn from ``codes``; optional extras: repeats (the same code again soon after the restart), cascade
secondaries, on-site technician events with a known response time, fleet-wide grid losses, data gaps
(communication-lost events) and one turbine with a higher failure rate.

Usage:
    log, truth = synthetic_log(n_turbines=20, years=4, process="hpp", mtbf_h=600, seed=1)
    log.to_csv("demo_log.csv", index=False)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from wind_om.schema import validate

CODES = {  # code: (description, component, weight)
    "1001": ("Pitch converter fault", "pitch", 0.30),
    "2002": ("Converter overtemperature", "converter", 0.25),
    "3003": ("Low hydraulic pressure", "brake_hydraulics", 0.15),
    "4004": ("Gearbox oil pressure low", "gearbox", 0.10),
    "5005": ("Yaw motor overload", "yaw", 0.10),
    "6006": ("Anemometer defect", "sensors", 0.10),
}
SECONDARY = {"1001": "7101", "2002": "7202"}  # code -> its usual secondary alarm
MAPPING_ROWS = [
    *[(c, comp, "", "forced_outage", "1", d) for c, (d, comp, _) in CODES.items()],
    ("7101", "pitch", "", "forced_outage", "1", "Pitch axis 2 not ready"),
    ("7202", "converter", "", "forced_outage", "1", "DC link voltage high"),
    ("20", "service", "onsite", "scheduled_maintenance", "1", "Manual stop - on site"),
    ("3500", "grid", "", "grid", "1", "Grid loss"),
    ("9997", "control", "", "communication", "0", "Data communication unavailable"),
]


@dataclass
class Truth:
    process: str
    mtbf_h: float = np.nan
    shape: float = np.nan
    scale_h: float = np.nan
    beta: float = np.nan
    lam: float = np.nan
    ttr_median_h: float = np.nan
    ttr_sigma: float = np.nan
    ttr_mean_h: float = np.nan
    response_h: float = np.nan
    bad_turbine: str | None = None
    repeat_starts: list = field(default_factory=list)
    origin: dict = field(default_factory=dict)
    window: tuple = ()
    gaps: list = field(default_factory=list)


def mapping_frame() -> pd.DataFrame:
    return pd.DataFrame(MAPPING_ROWS, columns=["code", "component", "role", "category", "stopping", "description"])


def _power_law(rng, lam, beta, a, b) -> np.ndarray:
    """NHPP power-law event ages in (a, b] by inversion of the cumulative intensity."""
    n = rng.poisson(lam * (b**beta - a**beta))
    u = np.sort(rng.uniform(a**beta, b**beta, n))
    return u ** (1 / beta)


def synthetic_log(n_turbines: int = 20, years: float = 4.0, process: str = "hpp", mtbf_h: float = 600.0,
                  shape: float = 1.0, scale_h: float = 600.0, beta: float = 1.0, lam: float | None = None,
                  ttr_median_h: float = 4.0, ttr_sigma: float = 1.0, start: str = "2020-01-01",
                  age_at_start_h: float = 0.0, repeat_prob: float = 0.0, repeat_days: float = 2.0,
                  cascade_prob: float = 0.0, onsite_prob: float = 0.0, response_h: float = 6.0,
                  grid_losses: int = 0, gaps: int = 0, bad_factor: float = 1.0,
                  seed: int = 0) -> tuple[pd.DataFrame, Truth]:
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start, tz="UTC")
    horizon = years * 8766.0
    t1 = t0 + pd.Timedelta(hours=horizon)
    codes = list(CODES)
    w = np.array([CODES[c][2] for c in codes])
    w = w / w.sum()
    mu = np.log(ttr_median_h)
    truth = Truth(process, ttr_median_h=ttr_median_h, ttr_sigma=ttr_sigma,
                  ttr_mean_h=float(np.exp(mu + ttr_sigma**2 / 2)), window=(t0, t1),
                  response_h=response_h if onsite_prob else np.nan)
    if lam is None and process == "power_law":
        # same expected count as an HPP with mtbf_h over the window
        a, b = age_at_start_h, age_at_start_h + horizon
        lam = (horizon / mtbf_h) / (b**beta - a**beta)
    truth.mtbf_h, truth.shape, truth.scale_h, truth.beta, truth.lam = (
        mtbf_h if process == "hpp" else np.nan, shape if process == "weibull" else np.nan,
        scale_h if process == "weibull" else np.nan, beta if process == "power_law" else np.nan,
        lam if process == "power_law" else np.nan)
    rows = []
    tids = [f"WTG{i + 1:02d}" for i in range(n_turbines)]
    if bad_factor != 1.0:
        truth.bad_turbine = tids[0]
    for tid in tids:
        truth.origin[tid] = t0 - pd.Timedelta(hours=age_at_start_h)
        factor = bad_factor if tid == truth.bad_turbine else 1.0
        # failure times on the operating clock (hours from the window start)
        if process == "hpp":
            n = rng.poisson(horizon * factor / mtbf_h)
            op = np.sort(rng.uniform(0, horizon, n))
        elif process == "weibull":
            op, t = [], 0.0
            while True:
                t += scale_h / factor * rng.weibull(shape)
                if t >= horizon:
                    break
                op.append(t)
            op = np.array(op)
        elif process == "power_law":
            op = _power_law(rng, lam * factor, beta, age_at_start_h, age_at_start_h + horizon) - age_at_start_h
        else:
            raise ValueError(process)
        ttr = np.exp(rng.normal(mu, ttr_sigma, len(op)))
        if process == "power_law":
            ttr = np.minimum(ttr, 2.0)  # keep calendar age = operating age
        cal = op + np.r_[0, np.cumsum(ttr)[:-1]]  # calendar hours: operating clock + earlier downtime
        for k, (s, d) in enumerate(zip(cal, ttr)):
            if s >= horizon:
                break
            code = rng.choice(codes, p=w)
            st = t0 + pd.Timedelta(hours=float(s))
            en = min(st + pd.Timedelta(hours=float(d)), t1)
            rows.append((tid, st, en, code, CODES[code][0], "forced_outage", True))
            if rng.random() < repeat_prob:  # reset without a fix: the same fault again soon after the restart
                rs = en + pd.Timedelta(days=float(rng.uniform(0.05, repeat_days)))
                if rs < t1:
                    rows.append((tid, rs, min(rs + pd.Timedelta(hours=float(np.exp(rng.normal(mu, ttr_sigma)))),
                                              t1), code, CODES[code][0], "forced_outage", True))
                    truth.repeat_starts.append((tid, rs))
            if code in SECONDARY and rng.random() < cascade_prob:
                rows.append((tid, st + pd.Timedelta(seconds=20), en, SECONDARY[code], "secondary", "forced_outage",
                             True))
            if rng.random() < onsite_prob and d > response_h:
                on = st + pd.Timedelta(hours=response_h)
                rows.append((tid, on, en, "20", "Manual stop - on site", "scheduled_maintenance", True))
    for g in range(grid_losses):
        s = t0 + pd.Timedelta(hours=float(rng.uniform(0.05, 0.95) * horizon))
        for tid in tids:
            st = s + pd.Timedelta(seconds=float(rng.uniform(0, 30)))
            rows.append((tid, st, st + pd.Timedelta(hours=1.5), "3500", "Grid loss", "grid", True))
    log = pd.DataFrame(rows, columns=["turbine_id", "start", "end", "code", "description", "category", "stop"])
    for g in range(gaps):
        tid = tids[g % n_turbines]
        gs = t0 + pd.Timedelta(hours=float(rng.uniform(0.2, 0.8) * horizon))
        ge = gs + pd.Timedelta(days=30)
        drop = (log["turbine_id"] == tid) & (log["start"] < ge) & (log["end"] > gs)
        gap_row = pd.DataFrame([(tid, gs, ge, "9997", "Data communication unavailable", "communication", False)],
                               columns=log.columns)
        log = pd.concat([log[~drop], gap_row], ignore_index=True)
        truth.gaps.append((tid, gs, ge))
    return validate(log.reset_index(drop=True), "synthetic"), truth
