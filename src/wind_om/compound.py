"""Compound events: repeat failures, alarm cascades, resets versus interventions, service response time.

* **Repeat failure** - a failure whose code (or component) already failed on the same turbine within ``days``
  after the previous one was restored. Repeats form chains; the repeat rate is the share of failures that are
  repeats - the usual sign of a reset without a fix.
* **Cascade** - the alarms that start within ``window`` after the first-up stop of an episode on the same
  turbine: stops, and alarms with a duration that are not plain information, environment, standby or
  communication messages; events with a role (someone's manual action) are left out on both sides. The
  first-up is the primary alarm, the rest are secondary. Pair statistics say how often a primary drags a given
  secondary along (``confidence``) and how much more than by chance (``lift``).
* **Restoration** - how a failure ended: ``on_site`` (an event with role *onsite* during the episode or shortly
  after: manual stop on site, service mode, manual operation), ``remote`` (role *remote*: remote stop or
  reset), ``auto_reset`` (no such event and restored within ``auto_max``) or ``not_logged`` (longer, but nothing
  in the log says who restored it). The roles come from the code mapping.
* **Response time** - from the failure start to the first on-site event. When that event comes within
  ``present_window`` of the failure (or before it), someone was already there - often the failure happened
  during the work - and the failure is flagged ``technician_present`` instead.

Usage:
    rep = repeats(fail, days=7)
    cas, pairs = cascades(ev, ep, window="2min")
    res = restoration(fail, ev, post="30min", auto_max="20min", present_window="15min")
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from wind_om.events import NS_H, _ns

QUIET = ("information", "environmental", "technical_standby", "communication")


def repeats(fail: pd.DataFrame, days: float = 7.0, by: str = "code") -> pd.DataFrame:
    """Failures with repeat flags: days_since_previous, is_repeat, chain_id, chain_pos."""
    f = fail.sort_values(["turbine_id", by, "start"]).copy()
    prev_end = f.groupby(["turbine_id", by])["end"].shift(1)
    f["days_since_previous"] = (f["start"] - prev_end).dt.total_seconds() / 86400
    f["is_repeat"] = f["days_since_previous"].le(days)
    f["chain_id"] = (~f["is_repeat"]).cumsum() - 1
    f["chain_pos"] = f.groupby("chain_id").cumcount()
    return f.sort_values(["turbine_id", "start"]).reset_index(drop=True)


def repeat_summary(rep: pd.DataFrame, by: str = "code", operating_h: float | None = None,
                   days: float = 7.0) -> pd.DataFrame:
    """Per code: failures, repeats, repeat rate, longest chain and the hours lost in repeats.

    With the fleet's ``operating_h``, ``chance_rate`` is the repeat rate expected if the code simply recurred
    at its average rate (Poisson): 1 - exp(-rate x days). A repeat rate well above it points to resets
    without a fix; near it, the "repeats" are just a frequent code.
    """
    chains = rep.groupby("chain_id").size()
    rep = rep.assign(chain_len=rep["chain_id"].map(chains))
    g = rep.groupby(by).agg(failures=("is_repeat", "size"), repeats=("is_repeat", "sum"),
                            longest_chain=("chain_len", "max"),
                            repeat_downtime_h=("duration_h", lambda s: s[rep.loc[s.index, "is_repeat"]].sum()))
    if "description" in rep:
        g["description"] = rep.groupby(by)["description"].agg(lambda s: s.mode().iloc[0] if len(s) else "")
    g["repeat_rate"] = g["repeats"] / g["failures"]
    if operating_h:
        n_turb = rep["turbine_id"].nunique()
        per_turbine_h = operating_h / max(n_turb, 1)
        g["chance_rate"] = 1 - np.exp(-g["failures"] / n_turb / per_turbine_h * days * 24)
        g["excess"] = g["repeat_rate"] - g["chance_rate"]
    return g.sort_values(["repeats", "failures"], ascending=False).reset_index()


def cascades(ev: pd.DataFrame, ep: pd.DataFrame, window="2min") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Primary (first-up) and secondary alarms of every episode, and primary -> secondary pair statistics."""
    w = pd.Timedelta(window).value
    role = ev["role"] if "role" in ev else pd.Series("", index=ev.index)
    alarm = (ev["end"] > ev["start"]) & ~ev["category"].isin(QUIET)
    member = ev[(ev["stop"].astype(bool) | alarm) & role.eq("")]
    rows = []
    for tid, e in ep.groupby("turbine_id"):
        m = member[member["turbine_id"] == tid].sort_values(["start", "seq"])
        ms, idx = _ns(m["start"]), m.index.to_numpy()
        for fi, s0 in zip(e["first_idx"].to_numpy(), _ns(e["start"])):
            lo, hi = np.searchsorted(ms, s0, side="left"), np.searchsorted(ms, s0 + w, side="right")
            sec = [i for i in idx[lo:hi] if i != fi]
            rows.append((tid, fi, ev.at[fi, "code"], role.at[fi], len(sec), [ev.at[i, "code"] for i in sec]))
    cas = pd.DataFrame(rows, columns=["turbine_id", "first_idx", "primary", "primary_role", "n_secondary",
                                      "secondary"])
    cas = cas.merge(ep[["first_idx", "episode_id", "start", "duration_h"]], on="first_idx", how="left")
    desc = ev.groupby("code")["description"].agg(lambda s: s.mode().iloc[0] if len(s) else "")
    return cas, cascade_pairs(cas, desc)


def cascade_pairs(cas: pd.DataFrame, desc: pd.Series | None = None) -> pd.DataFrame:
    """Primary -> secondary pairs: count (episodes), confidence = count / episodes of the primary, lift."""
    cols = ["primary", "secondary", "count", "primary_episodes", "confidence", "lift"]
    if "primary_role" in cas:
        cas = cas[cas["primary_role"].eq("")]  # a manual stop opening an episode is not an alarm cascade
    # one count per episode and pair
    ex = cas[["episode_id", "primary", "secondary"]].explode("secondary").dropna().drop_duplicates()
    if ex.empty:
        return pd.DataFrame(columns=cols)
    n_primary = cas.groupby("primary").size()
    base = ex.groupby("secondary")["episode_id"].nunique() / len(cas)
    pairs = ex.groupby(["primary", "secondary"]).size().rename("count").reset_index()
    pairs["primary_episodes"] = pairs["primary"].map(n_primary)
    pairs["confidence"] = pairs["count"] / pairs["primary_episodes"]
    pairs["lift"] = pairs["confidence"] / pairs["secondary"].map(base)
    if desc is not None:
        pairs.insert(1, "primary_text", pairs["primary"].map(desc).fillna(""))
        pairs.insert(3, "secondary_text", pairs["secondary"].map(desc).fillna(""))
    return pairs.sort_values(["count", "confidence"], ascending=False).reset_index(drop=True)


def restoration(fail: pd.DataFrame, ev: pd.DataFrame, post="30min", auto_max="20min",
                present_window="15min") -> pd.DataFrame:
    """How each failure was restored and the service response time (see module docstring)."""
    post, auto_max, pw = pd.Timedelta(post).value, pd.Timedelta(auto_max), pd.Timedelta(present_window).value
    roles = ev[ev["role"].isin(["onsite", "remote"])] if "role" in ev else ev.iloc[:0]
    out = fail.copy()
    cls = np.full(len(out), "not_logged", dtype=object)
    resp = np.full(len(out), np.nan)
    remote = np.full(len(out), np.nan)
    present = np.zeros(len(out), dtype=bool)
    for tid, f in out.groupby("turbine_id"):
        r = roles[roles["turbine_id"] == tid]
        rs, re_, rr = _ns(r["start"]), _ns(r["end"]), r["role"].to_numpy()
        pos = out.index.get_indexer(f.index)
        for p, s, e in zip(pos, _ns(f["start"]), _ns(f["end"])):
            hit = (rs <= e + post) & (np.maximum(re_, rs) >= s)  # active at some point of [s, e + post]
            if not hit.any():
                continue
            on = hit & (rr == "onsite")
            if on.any():
                cls[p] = "on_site"
                first = rs[on].min()
                present[p] = first < s + pw
                resp[p] = np.nan if present[p] else (first - s) / NS_H
            else:
                cls[p] = "remote"
                remote[p] = max(rs[hit].min() - s, 0) / NS_H
    short = (cls == "not_logged") & (out["duration_h"].to_numpy() <= auto_max.total_seconds() / 3600)
    cls[short] = "auto_reset"
    out["restoration"], out["response_h"], out["remote_after_h"] = cls, resp, remote
    out["technician_present"] = present
    return out


def restoration_summary(res: pd.DataFrame) -> pd.DataFrame:
    """Share of each restoration class, its median restore time and (on site) the response-time quantiles."""
    out = res.groupby("restoration")["duration_h"].agg(failures="size", restore_median_h="median",
                                                        downtime_h="sum")
    out = out.reindex(["auto_reset", "remote", "on_site", "not_logged"]).dropna(how="all")
    out["share"] = out["failures"] / out["failures"].sum()
    rt = res.loc[(res["restoration"] == "on_site") & ~res["technician_present"], "response_h"]
    out["response_median_h"], out["response_p90_h"] = np.nan, np.nan
    if "on_site" in out.index and len(rt):
        out.loc["on_site", ["response_median_h", "response_p90_h"]] = rt.median(), rt.quantile(0.9)
    return out.reset_index()
