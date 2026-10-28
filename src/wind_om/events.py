"""From logged events to stop episodes, attributed downtime, observation windows and reporting periods.

* **Open events.** A stop without an end lasts until the next stop of the same turbine starts (or the end of the
  window); an instantaneous event without an end lasts zero time. Imputed ends are flagged.
* **Episodes.** The stops of one turbine that overlap, or follow each other within ``merge_gap`` (a restart
  attempt that fails again), form one downtime episode: one repair from the first stop to the final restart.
* **Attribution.** Overlapping stops must not count twice. Each moment of downtime belongs to the *first-up*
  event: the earliest-starting stop still active (ties: the one listed first). Pareto hours use this; an
  event fully inside an earlier one gets zero hours (it is a consequence, see ``compound.cascades``).
* **Dominant cause.** The episode's cause is its event with the most attributed hours; its first-up event
  is kept as well.
* **Fleet-wide episodes.** Episodes that start within ``window`` on at least half of the turbines (and at least
  three) share a common cause - grid loss, park controller, storm - and are flagged.
* **Exposure.** Observation windows per turbine, minus periods without information (communication lost), are
  the time during which failures could be seen.

Usage:
    ev = close_events(log, window_end=t1)
    ep, seg = episodes(ev)                  # one row per episode / per attributed downtime segment
    ep = flag_fleet_wide(ep, n_turbines=6)
    exp = exposure(ev, t0, t1)              # turbine_id, start, end
    hours = overlap_hours(seg["start"], seg["end"], a, b)
"""
from __future__ import annotations

import heapq
import logging

import numpy as np
import pandas as pd

from wind_om.schema import FAILURE_CATEGORIES

log = logging.getLogger(__name__)
MERGE_GAP = pd.Timedelta("10min")
NS_H = 3.6e12


def _ns(s) -> np.ndarray:
    """UTC datetimes -> int64 nanoseconds."""
    return pd.DatetimeIndex(pd.to_datetime(s, utc=True)).as_unit("ns").asi8


def _ts(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(np.asarray(ns, dtype="int64"), utc=True)


def close_events(df: pd.DataFrame, window_end=None, max_open=None) -> pd.DataFrame:
    """Give every event an end: open stops run to the next stop of the turbine, other events last zero time."""
    out = df.copy()
    out["end_imputed"] = out["end"].isna()
    stop = out["stop"].astype(bool)
    if window_end is None:
        window_end = out["start"].max()
    window_end = pd.Timestamp(window_end).tz_convert("UTC") if pd.Timestamp(window_end).tzinfo else \
        pd.Timestamp(window_end).tz_localize("UTC")
    nxt = out.loc[stop].groupby("turbine_id")["start"].shift(-1)
    open_stop = stop & out["end"].isna()
    fill = nxt.reindex(out.index)[open_stop].fillna(window_end)
    if max_open is not None:
        fill = fill.where(fill - out.loc[open_stop, "start"] <= pd.Timedelta(max_open),
                          out.loc[open_stop, "start"] + pd.Timedelta(max_open))
    out.loc[open_stop, "end"] = fill
    out.loc[~stop & out["end"].isna(), "end"] = out.loc[~stop & out["end"].isna(), "start"]
    out["end"] = out[["start", "end"]].max(axis=1)
    if open_stop.any():
        log.info("%d open stops closed at the next stop of the turbine (or the window end)", int(open_stop.sum()))
    return out


def _attribute(starts: np.ndarray, ends: np.ndarray) -> list[tuple[int, int, int]]:
    """First-up attribution of the union of [start, end) intervals sorted by start: (a, b, owner) segments."""
    bounds = np.unique(np.concatenate([starts, ends]))
    heap: list[int] = []
    out: list[tuple[int, int, int]] = []
    j, n = 0, len(starts)
    for k in range(len(bounds) - 1):
        b = bounds[k]
        while j < n and starts[j] <= b:
            if ends[j] > starts[j]:
                heapq.heappush(heap, j)
            j += 1
        while heap and ends[heap[0]] <= b:
            heapq.heappop(heap)
        if not heap:
            continue
        owner = heap[0]
        if out and out[-1][2] == owner and out[-1][1] == b:
            out[-1] = (out[-1][0], int(bounds[k + 1]), owner)
        else:
            out.append((int(b), int(bounds[k + 1]), owner))
    return out


def episodes(df: pd.DataFrame, merge_gap=MERGE_GAP,
             failure_categories=FAILURE_CATEGORIES) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stops -> (episodes, attributed segments). ``df`` must have closed ends (``close_events``).

    Episodes: episode_id, turbine_id, start, end, duration_h (first stop to restart), stop_h (union),
    n_events, first_idx / first_code (first-up), failure, cause_idx / code / component / category.
    The cause is the failure-category stop with the most attributed hours when the episode has one (then
    ``failure`` is True: at some moment a fault was the reason the turbine stood still - a technician's manual
    stop that follows does not hide it), otherwise the stop with the most attributed hours.
    Segments: turbine_id, start, end, hours, event index (``idx``, index of ``df``) and episode_id. The
    attributed hours of every stop are ``seg.groupby("idx")["hours"].sum()``.
    """
    st = df[df["stop"].astype(bool) & (df["end"] > df["start"])]
    gap = pd.Timedelta(merge_gap).value
    ep_rows, seg_rows = [], []
    eid = 0
    for tid, g in st.sort_values(["turbine_id", "start", "seq"], kind="stable").groupby("turbine_id", sort=True):
        s, e, idx = _ns(g["start"]), _ns(g["end"]), g.index.to_numpy()
        segs = _attribute(s, e)
        # episodes: union with the merge gap
        run_end = np.maximum.accumulate(e)
        new = np.r_[True, s[1:] > run_end[:-1] + gap]
        ep_of = np.cumsum(new) - 1 + eid
        seg_ep = ep_of[[o for _, _, o in segs]]
        for (a, b, o), ep in zip(segs, seg_ep):
            seg_rows.append((tid, a, b, idx[o], ep))
        for k in np.unique(ep_of):
            m = ep_of == k
            first = np.flatnonzero(m)[0]
            ep_rows.append((k, tid, s[m].min(), e[m].max(), int(m.sum()), idx[first]))
        eid = ep_of[-1] + 1
    seg = pd.DataFrame(seg_rows, columns=["turbine_id", "a", "b", "idx", "episode_id"])
    seg["hours"] = (seg["b"] - seg["a"]) / NS_H
    seg["start"], seg["end"] = _ts(seg.pop("a").to_numpy()), _ts(seg.pop("b").to_numpy())
    seg = seg[["turbine_id", "start", "end", "hours", "idx", "episode_id"]]
    ep = pd.DataFrame(ep_rows, columns=["episode_id", "turbine_id", "a", "b", "n_events", "first_idx"])
    ep["start"], ep["end"] = _ts(ep.pop("a").to_numpy()), _ts(ep.pop("b").to_numpy())
    ep["duration_h"] = (ep["end"] - ep["start"]).dt.total_seconds() / 3600
    ep["stop_h"] = seg.groupby("episode_id")["hours"].sum().reindex(ep["episode_id"]).fillna(0).to_numpy()
    if len(seg):
        by = seg.groupby(["episode_id", "idx"])["hours"].sum().reset_index()
        by["is_failure"] = df.loc[by["idx"], "category"].isin(failure_categories).to_numpy() & (by["hours"] > 0)
        by = by.sort_values(["episode_id", "is_failure", "hours"], ascending=[True, False, False], kind="stable")
        top = by.drop_duplicates("episode_id").set_index("episode_id")
        ep["cause_idx"] = top["idx"].reindex(ep["episode_id"]).to_numpy()
        ep["failure"] = top["is_failure"].reindex(ep["episode_id"]).fillna(False).to_numpy(dtype=bool)
    else:
        ep["cause_idx"] = pd.Series(dtype="int64")
        ep["failure"] = pd.Series(dtype=bool)
    for c in ("code", "description", "category", "component"):
        if c in df.columns:
            ep[c] = df.loc[ep["cause_idx"], c].to_numpy() if len(ep) else pd.Series(dtype=object)
    ep["first_code"] = df.loc[ep["first_idx"], "code"].to_numpy() if len(ep) else pd.Series(dtype=object)
    cols = ["episode_id", "turbine_id", "start", "end", "duration_h", "stop_h", "n_events", "first_idx",
            "first_code", "failure", "cause_idx"] + [c for c in ("code", "description", "category", "component")
                                                     if c in ep]
    return ep[cols], seg


def flag_fleet_wide(ep: pd.DataFrame, n_turbines: int, window="5min", min_share: float = 0.5,
                    min_turbines: int = 3) -> pd.DataFrame:
    """Flag episodes that start together on many turbines (common cause); adds fleet_wide and fleet_group."""
    out = ep.copy()
    out["fleet_wide"], out["fleet_group"] = False, -1
    if out.empty:
        return out
    need = max(min_turbines, int(np.ceil(min_share * n_turbines)))
    order = np.argsort(_ns(out["start"]), kind="stable")
    s = _ns(out["start"])[order]
    tids = out["turbine_id"].to_numpy()[order]
    w = pd.Timedelta(window).value
    grp, g0, gid = np.zeros(len(s), dtype=int), s[0], 0
    for k in range(1, len(s)):
        if s[k] > g0 + w:
            gid += 1
            g0 = s[k]
        grp[k] = gid
    df = pd.DataFrame({"g": grp, "t": tids})
    size = df.groupby("g")["t"].nunique()
    big = size[size >= need].index
    fw = np.isin(grp, big)
    flags = np.zeros(len(out), dtype=bool)
    groups = np.full(len(out), -1)
    flags[order], groups[order] = fw, np.where(fw, grp, -1)
    out["fleet_wide"], out["fleet_group"] = flags, groups
    return out


# ------------------------------------------------------------------------------------- interval arithmetic

def union(starts: np.ndarray, ends: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Union of [start, end) intervals (int64 ns), sorted and merged."""
    if len(starts) == 0:
        return np.array([], dtype="int64"), np.array([], dtype="int64")
    o = np.argsort(starts, kind="stable")
    s, e = np.asarray(starts)[o], np.asarray(ends)[o]
    run_end = np.maximum.accumulate(e)
    new = np.r_[True, s[1:] > run_end[:-1]]
    last = np.r_[np.flatnonzero(new)[1:] - 1, len(s) - 1]  # the running max at a group's last member is its end
    return s[new], run_end[last]


def subtract(a_s, a_e, b_s, b_e) -> tuple[np.ndarray, np.ndarray]:
    """Intervals A minus the union of intervals B (all int64 ns)."""
    b_s, b_e = union(np.asarray(b_s), np.asarray(b_e))
    out_s, out_e = [], []
    for s, e in zip(a_s, a_e):
        cur = s
        k0 = np.searchsorted(b_e, s, side="right")
        for k in range(k0, len(b_s)):
            if b_s[k] >= e:
                break
            if b_s[k] > cur:
                out_s.append(cur)
                out_e.append(b_s[k])
            cur = max(cur, b_e[k])
        if cur < e:
            out_s.append(cur)
            out_e.append(e)
    return np.array(out_s, dtype="int64"), np.array(out_e, dtype="int64")


def overlap_hours(starts, ends, a, b) -> np.ndarray:
    """Hours of each [start, end) inside [a, b)."""
    s, e = _ns(starts), _ns(ends)
    a, b = pd.Timestamp(a).value, pd.Timestamp(b).value
    return np.clip(np.minimum(e, b) - np.maximum(s, a), 0, None) / NS_H


def exposure(ev: pd.DataFrame, t0, t1, coverage: pd.DataFrame | None = None,
             gaps_from_log: bool = True, min_gap="6h", max_silence=None) -> pd.DataFrame:
    """Observation windows per turbine (turbine_id, start, end, hours).

    ``coverage`` (turbine_id, start, end) lists the periods with valid logging; without it every turbine is
    observed from ``t0`` to ``t1``. Events of category ``communication`` lasting ``min_gap`` or longer are cut
    out (no information); shorter drop-outs of the data link do not interrupt the controller's own log.
    ``max_silence`` (e.g. "7D"): a turbine that logs nothing at all for longer than this is taken as not
    logged in that time - useful for logs that also hold routine messages (start-up, low wind), where weeks of
    silence mean missing files, not a perfect turbine.
    """
    turbines = sorted(ev["turbine_id"].unique()) if coverage is None else sorted(coverage["turbine_id"].unique())
    t0, t1 = pd.Timestamp(t0), pd.Timestamp(t1)
    rows = []
    for tid in turbines:
        if coverage is None:
            s, e = np.array([t0.value]), np.array([t1.value])
        else:
            c = coverage[coverage["turbine_id"] == tid]
            s, e = union(_ns(c["start"]), _ns(c["end"]))
            s, e = np.clip(s, t0.value, t1.value), np.clip(e, t0.value, t1.value)
            keep = e > s
            s, e = s[keep], e[keep]
        mine = ev[ev["turbine_id"] == tid]
        if gaps_from_log:
            g = mine[mine["category"].eq("communication") & (mine["end"] - mine["start"] >= pd.Timedelta(min_gap))]
            if len(g):
                s, e = subtract(s, e, _ns(g["start"]), _ns(g["end"]))
        if max_silence is not None:
            q = pd.Timedelta(max_silence).value
            us, ue = union(_ns(mine["start"]), np.maximum(_ns(mine["end"].fillna(mine["start"])), _ns(mine["start"])))
            edges_s = np.r_[t0.value, ue]
            edges_e = np.r_[us, t1.value]
            quiet = edges_e - edges_s > q
            if quiet.any():
                s, e = subtract(s, e, edges_s[quiet], edges_e[quiet])
        rows += [(tid, a, b) for a, b in zip(s, e) if b > a]
    out = pd.DataFrame(rows, columns=["turbine_id", "a", "b"])
    out["start"], out["end"] = _ts(out.pop("a").to_numpy()), _ts(out.pop("b").to_numpy())
    out["hours"] = (out["end"] - out["start"]).dt.total_seconds() / 3600
    return out


def periods(t0, t1, freq: str = "MS", tz: str = "UTC") -> pd.DataFrame:
    """Reporting periods (calendar months by default) in local time ``tz``, with their UTC bounds and hours.

    In Europe/Warsaw, March has 743 hours and October 745 (the DST changes).
    """
    t0, t1 = pd.Timestamp(t0).tz_convert(tz), pd.Timestamp(t1).tz_convert(tz)
    first = t0.tz_localize(None).to_period("M").to_timestamp() if freq == "MS" else t0.tz_localize(None).normalize()
    starts = pd.date_range(first, t1.tz_localize(None), freq=freq)
    starts = starts.append(pd.DatetimeIndex([starts[-1] + pd.tseries.frequencies.to_offset(freq)]))
    loc = starts.tz_localize(tz, ambiguous=np.zeros(len(starts), dtype=bool), nonexistent="shift_forward")
    utc = loc.tz_convert("UTC")
    out = pd.DataFrame({"period": [s.strftime("%Y-%m") if freq == "MS" else s.strftime("%Y-%m-%d")
                                   for s in starts[:-1]],
                        "start": utc[:-1], "end": utc[1:]})
    out["hours"] = (out["end"] - out["start"]).dt.total_seconds() / 3600
    return out


def period_of(times, tz: str = "UTC", freq: str = "M") -> pd.Series:
    """Label ('YYYY-MM') of the local-time period each UTC time falls in."""
    t = pd.to_datetime(pd.Series(times), utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    return t.dt.to_period(freq).astype(str)
