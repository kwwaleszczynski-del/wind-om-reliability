"""One call from a canonical log to every table: episodes, exposure, failures, compound events.

Usage:
    a = analyse(log, mapping=read_mapping("senvion_mm"), start="2024-01-01", end="2025-01-01")
    a.fail.head(); a.summary()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from wind_om import compound, events, reliability
from wind_om.mapping import apply_mapping
from wind_om.schema import FAILURE_CATEGORIES

log = logging.getLogger(__name__)


@dataclass
class Settings:
    merge_gap: str = "10min"
    fleet_window: str = "5min"
    fleet_share: float = 0.5
    repeat_days: float = 7.0
    repeat_by: str = "code"
    cascade_window: str = "2min"
    auto_max: str = "20min"
    present_window: str = "15min"
    restore_post: str = "30min"
    min_gap: str = "6h"
    max_silence: str | None = None
    min_failure_h: float = 0.0
    failure_categories: tuple = FAILURE_CATEGORIES
    default_stop: bool = False


@dataclass
class Analysis:
    ev: pd.DataFrame
    ep: pd.DataFrame
    seg: pd.DataFrame
    expo: pd.DataFrame
    up: pd.DataFrame
    fail: pd.DataFrame
    rep: pd.DataFrame
    cas: pd.DataFrame
    pairs: pd.DataFrame
    res: pd.DataFrame
    start: pd.Timestamp
    end: pd.Timestamp
    settings: Settings
    origin: dict = field(default_factory=dict)

    @property
    def turbines(self) -> list[str]:
        return sorted(self.expo["turbine_id"].unique())

    def summary(self) -> dict:
        f, up = self.fail, self.up
        hours = up["hours"].sum()
        obs_years = self.expo["hours"].sum() / reliability.HOURS_PER_YEAR
        return {"turbines": len(self.turbines), "observed_turbine_years": obs_years,
                "stop_episodes": len(self.ep), "failures": len(f),
                "fleet_wide_episodes": int(self.ep["fleet_wide"].sum()) if len(self.ep) else 0,
                "failures_per_turbine_year": len(f) / obs_years if obs_years else np.nan,
                "mtbf_h": hours / len(f) if len(f) else np.nan,
                "mttr_h": f["duration_h"].mean() if len(f) else np.nan,
                "ttr_median_h": f["duration_h"].median() if len(f) else np.nan,
                "failure_downtime_h": f["duration_h"].sum(),
                "repeat_rate": self.rep["is_repeat"].mean() if len(self.rep) else np.nan}


def analyse(log_df: pd.DataFrame, mapping: pd.DataFrame | None = None, start=None, end=None,
            coverage: pd.DataFrame | None = None, settings: Settings | None = None,
            origin: dict | None = None) -> Analysis:
    st = settings or Settings()
    ev = apply_mapping(log_df, mapping, default_stop=st.default_stop) if "component" not in log_df else log_df
    t0 = pd.Timestamp(start, tz="UTC") if start is not None and pd.Timestamp(start).tzinfo is None else \
        (pd.Timestamp(start) if start is not None else ev["start"].min())
    t1 = pd.Timestamp(end, tz="UTC") if end is not None and pd.Timestamp(end).tzinfo is None else \
        (pd.Timestamp(end) if end is not None else ev["end"].max())
    ev = events.close_events(ev, window_end=t1)
    ev = ev[(ev["start"] < t1) & (ev["end"] >= t0)]
    ep, seg = events.episodes(ev, st.merge_gap, st.failure_categories)
    n_t = coverage["turbine_id"].nunique() if coverage is not None else ev["turbine_id"].nunique()
    ep = events.flag_fleet_wide(ep, n_t, st.fleet_window, st.fleet_share)
    expo = events.exposure(ev, t0, t1, coverage, min_gap=st.min_gap, max_silence=st.max_silence)
    up = reliability.uptime(ep, expo)
    fail = reliability.failures(ep, min_duration_h=st.min_failure_h)
    fail = fail[(fail["start"] >= t0) & (fail["start"] < t1)].reset_index(drop=True)
    rep = compound.repeats(fail, st.repeat_days, st.repeat_by)
    cas, pairs = compound.cascades(ev, ep, st.cascade_window)
    res = compound.restoration(fail, ev, st.restore_post, st.auto_max, st.present_window)
    log.info("%d events, %d stop episodes (%d fleet-wide), %d failures on %d turbines",
             len(ev), len(ep), int(ep["fleet_wide"].sum()) if len(ep) else 0, len(fail), n_t)
    return Analysis(ev, ep, seg, expo, up, fail, rep, cas, pairs, res, t0, t1, st, origin or {})
