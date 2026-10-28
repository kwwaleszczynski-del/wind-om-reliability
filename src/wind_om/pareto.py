"""Pareto of downtime (and lost energy) by status code or component, for any period.

Hours are the *attributed* hours of ``events.episodes`` (first-up rule, no double counting of overlapping
stops), clipped to the period, so a stop that crosses a month boundary is split between the months.
``stops`` counts every logged stop that starts in the period, ``first_up`` the episodes it opened and
``failures`` the failure episodes it caused.

Usage:
    p = pareto(ev, seg, ep, key="component", start=a, end=b)
    p.head()   # key, description, hours, share, cum_share, stops, first_up, failures, lost_mwh
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from wind_om.events import _ns, overlap_hours


def pareto(ev: pd.DataFrame, seg: pd.DataFrame, ep: pd.DataFrame, key: str = "code", start=None, end=None,
           categories=None) -> pd.DataFrame:
    """Downtime Pareto by ``key`` ("code" or "component"). ``categories`` restricts to those reason categories.

    If ``seg`` has a ``lost_kwh`` column (``energy.attribute_lost``), lost energy is summed the same way.
    """
    a = pd.Timestamp(start) if start is not None else ev["start"].min()
    b = pd.Timestamp(end) if end is not None else max(ev["end"].max(), ev["start"].max())
    s = seg.copy()
    s["key"] = ev.loc[s["idx"], key].to_numpy()
    s["category"] = ev.loc[s["idx"], "category"].to_numpy()
    if categories is not None:
        s = s[s["category"].isin(categories)]
    h = overlap_hours(s["start"], s["end"], a, b)
    s["h_in"] = h
    if "lost_kwh" in s:
        frac = np.where(s["hours"] > 0, h / s["hours"].replace(0, np.nan), 0)
        s["lost_in"] = np.nan_to_num(s["lost_kwh"].to_numpy() * frac)
    grp = s.groupby("key")
    out = pd.DataFrame({"hours": grp["h_in"].sum()})
    if "lost_in" in s:
        out["lost_mwh"] = grp["lost_in"].sum() / 1000
    st = ev[ev["stop"].astype(bool)]
    in_p = (st["start"] >= a) & (st["start"] < b)
    if categories is not None:
        in_p &= st["category"].isin(categories)
    out["stops"] = st[in_p].groupby(key).size()
    e_in = ep[(ep["start"] >= a) & (ep["start"] < b)]
    out["first_up"] = ev.loc[e_in["first_idx"], key].value_counts()
    f_in = e_in[e_in["failure"].astype(bool)]
    if "fleet_wide" in f_in:
        f_in = f_in[~f_in["fleet_wide"]]
    out["failures"] = ev.loc[f_in["cause_idx"], key].value_counts()
    out = out.fillna(0)
    out = out[(out["hours"] > 0) | (out["stops"] > 0)]
    if key == "code":
        desc = ev.groupby("code")["description"].agg(lambda x: x.mode().iloc[0] if len(x) else "")
        out.insert(0, "description", desc.reindex(out.index).fillna(""))
        comp = ev.groupby("code")["component"].first() if "component" in ev else None
        if comp is not None:
            out.insert(1, "component", comp.reindex(out.index).fillna("unmapped"))
    out = out.sort_values(["hours", "stops"], ascending=False)
    tot = out["hours"].sum()
    out.insert(out.columns.get_loc("hours") + 1, "share", out["hours"] / tot if tot else 0.0)
    out.insert(out.columns.get_loc("share") + 1, "cum_share", out["share"].cumsum())
    for c in ("stops", "first_up", "failures"):
        out[c] = out[c].astype(int)
    return out.rename_axis(key).reset_index()


def vital_few(p: pd.DataFrame, share: float = 0.8) -> int:
    """How many items make up ``share`` of the downtime (the 80/20 check)."""
    if p.empty:
        return 0
    return int(np.searchsorted(p["cum_share"].to_numpy(), share - 1e-12) + 1)


def monthly_hours(seg: pd.DataFrame, ev: pd.DataFrame, per: pd.DataFrame, key: str = "category") -> pd.DataFrame:
    """Attributed stop hours per period and ``key`` (wide table: one row per period)."""
    k = ev.loc[seg["idx"], key].to_numpy()
    s, e = _ns(seg["start"]), _ns(seg["end"])
    rows = {}
    for _, p in per.iterrows():
        h = np.clip(np.minimum(e, p["end"].value) - np.maximum(s, p["start"].value), 0, None) / 3.6e12
        rows[p["period"]] = pd.Series(h).groupby(k).sum()
    return pd.DataFrame(rows).T.fillna(0.0).rename_axis("period")
