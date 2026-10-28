"""Reliability indicators from failure episodes: MTBF / MTTR, repair-time distributions, Weibull, Crow-AMSAA and
bad actors.

Definitions used throughout:

* **Failure** - a stop episode in which a stop of a failure category (``schema.FAILURE_CATEGORIES``, forced
  outage by default) was at some moment the reason the turbine stood still, not flagged fleet-wide (a common
  cause such as a grid loss is not a turbine failure).
* **Operating time** - observation time minus all stop time (failures cannot start while stopped).
* **MTBF** - operating hours / failures, with a two-sided chi-square interval for a time-truncated test.
* **MTTR** - mean time to *restore*: from the first stop of the episode to the restart, so it includes waiting,
  travel and logistics, not only hands-on repair time.
* **Weibull** - fitted by maximum likelihood to the operating times between failures of each turbine (a renewal
  view); the last interval of every observation window is right-censored, the first is dropped (its start is
  unknown). Shape < 1: early failures / repeats, ~1: random, > 1: wear-out.
* **Crow-AMSAA** - the power-law non-homogeneous Poisson process, intensity
  ``lambda * beta * t**(beta - 1)`` in turbine age ``t``, fitted by maximum likelihood over all turbines and all
  their observation windows (gaps and late starts handled exactly). ``beta < 1``: failures thin out
  (reliability improves), ``beta > 1``: they become more frequent. The verdict comes from the likelihood-ratio
  test of beta = 1; the Laplace test on each turbine's observed-time clock is a model-free second opinion
  (it does not depend on the age origin).
* **Bad actors** - turbines (and code x turbine pairs) with more failures than the fleet rate explains
  (exact Poisson test, Benjamini-Hochberg false discovery rate), and the jack-knife diagram of Knights (2001):
  frequency against mean repair time, with acute, chronic and acute + chronic quadrants.

Usage:
    fail = failures(ep)
    up = uptime(ep, expo)
    kpi = mtbf_mttr(fail, up, by=["turbine_id"])
    w = weibull_fit(*tbf(fail, up))
    ca = crow_amsaa(fail, expo, origin={"T1": cod, ...})
    bad = turbine_outliers(fail, up)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import optimize, stats

from wind_om.events import NS_H, _ns, subtract

log = logging.getLogger(__name__)
HOURS_PER_YEAR = 8766.0


def failures(ep: pd.DataFrame, exclude_fleet_wide: bool = True, min_duration_h: float = 0.0) -> pd.DataFrame:
    """Failure episodes (see module docstring and ``events.episodes``)."""
    m = ep["failure"].astype(bool) & (ep["duration_h"] >= min_duration_h)
    if exclude_fleet_wide and "fleet_wide" in ep:
        m &= ~ep["fleet_wide"]
    return ep[m].sort_values(["turbine_id", "start"]).reset_index(drop=True)


def uptime(ep: pd.DataFrame, expo: pd.DataFrame) -> pd.DataFrame:
    """Operating intervals per turbine: observation windows minus all stop episodes (turbine_id, start, end)."""
    rows = []
    for tid, w in expo.groupby("turbine_id"):
        e = ep[ep["turbine_id"] == tid]
        s, t = subtract(_ns(w["start"]), _ns(w["end"]), _ns(e["start"]), _ns(e["end"]))
        rows.append(pd.DataFrame({"turbine_id": tid, "a": s, "b": t, "window": _window_of(s, w)}))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["turbine_id", "a", "b", "window"])
    out["start"] = pd.to_datetime(out.pop("a").to_numpy(dtype="int64"), utc=True)
    out["end"] = pd.to_datetime(out.pop("b").to_numpy(dtype="int64"), utc=True)
    out["hours"] = (out["end"] - out["start"]).dt.total_seconds() / 3600
    return out


def _window_of(s: np.ndarray, w: pd.DataFrame) -> np.ndarray:
    ws = _ns(w["start"].sort_values())
    return np.searchsorted(ws, s, side="right") - 1


def mtbf_ci(hours: float, n: int, conf: float = 0.9) -> tuple[float, float]:
    """Two-sided chi-square interval of the MTBF for a time-truncated observation."""
    a = 1 - conf
    lo = 2 * hours / stats.chi2.ppf(1 - a / 2, 2 * n + 2)
    hi = 2 * hours / stats.chi2.ppf(a / 2, 2 * n) if n > 0 else np.inf
    return lo, hi


def mtbf_mttr(fail: pd.DataFrame, up: pd.DataFrame, by=("turbine_id",), conf: float = 0.9,
              n_turbines: int | None = None) -> pd.DataFrame:
    """Failures, operating hours, MTBF (with interval), failure rate and restore-time statistics per group.

    Grouping by ``component`` (or anything not tied to a turbine) uses the operating hours of the whole fleet,
    since every turbine carries every component.
    """
    by = list(by)
    per_turbine = "turbine_id" in by
    up_h = up.groupby("turbine_id")["hours"].sum()
    if per_turbine:
        keys = fail[by].drop_duplicates() if len(by) > 1 else pd.DataFrame({"turbine_id": up_h.index})
    else:
        keys = fail[by].drop_duplicates()
    rows = []
    for _, k in keys.iterrows():
        m = np.ones(len(fail), dtype=bool)
        for c in by:
            m &= fail[c].to_numpy() == k[c]
        f = fail[m]
        hours = float(up_h.get(k["turbine_id"], 0.0)) if per_turbine else float(up_h.sum())
        n = len(f)
        lo, hi = mtbf_ci(hours, n, conf)
        d = f["duration_h"].to_numpy()
        rows.append({**{c: k[c] for c in by}, "failures": n, "operating_h": hours,
                     "mtbf_h": hours / n if n else np.nan, "mtbf_lo_h": lo, "mtbf_hi_h": hi,
                     "failures_per_turbine_year": n / hours * HOURS_PER_YEAR if hours else np.nan,
                     "mttr_h": d.mean() if n else np.nan, "ttr_median_h": np.median(d) if n else np.nan,
                     "ttr_p90_h": np.percentile(d, 90) if n else np.nan, "downtime_h": d.sum()})
    out = pd.DataFrame(rows)
    return out.sort_values("downtime_h", ascending=False).reset_index(drop=True) if len(out) else out


def lognormal_fit(durations_h) -> dict:
    """Lognormal restore-time model (MLE): median, sigma, mean and a Kolmogorov-Smirnov p-value."""
    d = np.asarray(durations_h, dtype=float)
    d = d[d > 0]
    if len(d) < 3:
        return {"n": len(d), "median_h": np.nan, "sigma": np.nan, "mean_h": np.nan, "ks_p": np.nan}
    lx = np.log(d)
    mu, sigma = lx.mean(), lx.std(ddof=0)
    ks = stats.kstest((lx - mu) / sigma, stats.norm.cdf).pvalue if sigma > 0 else np.nan
    return {"n": len(d), "median_h": float(np.exp(mu)), "sigma": float(sigma),
            "mean_h": float(np.exp(mu + sigma**2 / 2)), "ks_p": float(ks)}


# ------------------------------------------------------------------------------------------------ Weibull

def tbf(fail: pd.DataFrame, up: pd.DataFrame, group=None) -> tuple[np.ndarray, np.ndarray]:
    """Operating hours between failures (and right-censored tails) per turbine, pooled.

    Returns (times, observed): observed is False for the censored last interval of each observation window.
    ``group`` (a column of ``fail``, e.g. component) restricts to failures of one value per call when given
    as ``(column, value)``; the other failures still end the operating clock of nothing (independent risks).
    """
    times, obs = [], []
    for tid, u in up.groupby("turbine_id"):
        f = fail[fail["turbine_id"] == tid]
        if group is not None:
            f = f[f[group[0]] == group[1]]
        a, b, w = _ns(u["start"]), _ns(u["end"]), u["window"].to_numpy()
        cum = np.r_[0, np.cumsum(b - a)]
        for win in np.unique(w):
            sel = w == win
            ia = np.flatnonzero(sel)
            lo, hi = a[ia[0]], b[ia[-1]]
            fs = _ns(f["start"])
            fs = fs[(fs >= lo) & (fs < hi)]
            # operating clock at each failure start: uptime before it (failures start at an uptime end)
            k = np.searchsorted(a, fs, side="right") - 1
            clock = cum[k] + np.clip(fs - a[k], 0, b[k] - a[k])
            end_clock = cum[ia[-1] + 1]
            if len(clock) >= 2:
                times += list(np.diff(clock) / NS_H)
                obs += [True] * (len(clock) - 1)
            if len(clock) >= 1 and end_clock > clock[-1]:
                times.append((end_clock - clock[-1]) / NS_H)
                obs.append(False)
    t, o = np.array(times, dtype=float), np.array(obs, dtype=bool)
    keep = t > 0
    return t[keep], o[keep]


@dataclass
class WeibullFit:
    shape: float
    scale_h: float
    shape_lo: float
    shape_hi: float
    n_failures: int
    n_censored: int

    def as_dict(self) -> dict:
        return {"weibull_shape": self.shape, "weibull_shape_lo": self.shape_lo, "weibull_shape_hi": self.shape_hi,
                "weibull_scale_h": self.scale_h, "n_intervals": self.n_failures, "n_censored": self.n_censored}


def _weibull_profile(beta, t, o):
    r = o.sum()
    tb = t**beta
    eta_b = tb.sum() / r
    return r * np.log(beta) - r * np.log(eta_b) + (beta - 1) * np.log(t[o]).sum() - r


def weibull_fit(t, observed, conf: float = 0.9) -> WeibullFit:
    """Two-parameter Weibull by maximum likelihood with right censoring (profile likelihood interval)."""
    t, o = np.asarray(t, float), np.asarray(observed, bool)
    r = int(o.sum())
    if r < 3:
        return WeibullFit(np.nan, np.nan, np.nan, np.nan, r, int((~o).sum()))
    scale = t.max()
    t = t / scale  # the shape does not depend on the time unit; this keeps t**beta finite
    lt = np.log(t)

    def score(beta):
        tb = t**beta
        return r / beta + lt[o].sum() - r * (tb * lt).sum() / tb.sum()

    beta = _root(score, 1e-3, 50.0)
    if not np.isfinite(beta):
        log.warning("Weibull fit did not converge (%d failures)", r)
        return WeibullFit(np.nan, np.nan, np.nan, np.nan, r, int((~o).sum()))
    eta = ((t**beta).sum() / r) ** (1 / beta) * scale
    lmax = _weibull_profile(beta, t, o)
    cut = stats.chi2.ppf(conf, 1) / 2

    def f(b):
        return lmax - _weibull_profile(b, t, o) - cut

    lo = _root(f, 1e-3, beta)
    hi = _root(f, beta, 50.0)
    return WeibullFit(float(beta), float(eta), float(lo), float(hi), r, int((~o).sum()))


def _root(f, a: float, b: float) -> float:
    """Root of ``f`` in [a, b], NaN when there is no sign change (or a value is not finite)."""
    fa, fb = f(a), f(b)
    if not (np.isfinite(fa) and np.isfinite(fb)) or fa * fb > 0:
        return np.nan
    return optimize.brentq(f, a, b)


# ------------------------------------------------------------------------------------------- Crow-AMSAA

@dataclass
class CrowAMSAA:
    beta: float
    lam: float  # per hour**beta
    beta_lo: float
    beta_hi: float
    n: int
    systems: int
    p_trend_lr: float  # likelihood ratio test of beta = 1
    laplace_u: float
    p_laplace: float
    hours: float

    @property
    def verdict(self) -> str:
        """From the likelihood-ratio test of beta = 1 (5 %); the Laplace test is the model-free second opinion."""
        if self.n < 5:
            return "too few failures"
        if not np.isfinite(self.p_trend_lr):
            return "no power-law fit"
        if self.p_trend_lr >= 0.05:
            return "no significant trend"
        return "deteriorating" if self.beta > 1 else "improving"

    @property
    def laplace_agrees(self) -> bool:
        if self.verdict not in ("improving", "deteriorating") or not np.isfinite(self.p_laplace):
            return False
        return self.p_laplace < 0.05 and (self.laplace_u > 0) == (self.beta > 1)

    def intensity(self, age_h) -> np.ndarray:
        """Failures per hour per turbine at turbine age ``age_h``."""
        return self.lam * self.beta * np.asarray(age_h, float) ** (self.beta - 1)

    def as_dict(self) -> dict:
        return {"crow_beta": self.beta, "crow_beta_lo": self.beta_lo, "crow_beta_hi": self.beta_hi,
                "crow_lambda": self.lam, "failures": self.n, "turbines": self.systems,
                "observed_turbine_years": self.hours / HOURS_PER_YEAR, "p_trend_lr": self.p_trend_lr,
                "laplace_u": self.laplace_u, "p_laplace": self.p_laplace, "verdict": self.verdict,
                "laplace_agrees": self.laplace_agrees}


def nhpp_windows(fail: pd.DataFrame, expo: pd.DataFrame, origin=None) -> tuple[np.ndarray, np.ndarray,
                                                                               np.ndarray, np.ndarray]:
    """(failure ages, failure window ids, window starts, window ends) in hours of turbine age.

    ``origin``: {turbine_id: Timestamp} (e.g. commissioning dates); turbines without one start at the
    beginning of their first observation window.
    """
    origin = origin or {}
    ages, wid, ws, we = [], [], [], []
    k = 0
    for tid, w in expo.sort_values("start").groupby("turbine_id"):
        t0 = pd.Timestamp(origin.get(tid, w["start"].min())).value
        f = _ns(fail.loc[fail["turbine_id"] == tid, "start"])
        for a, b in zip(_ns(w["start"]), _ns(w["end"])):
            sel = f[(f >= a) & (f < b)]
            ws.append(max(a - t0, 0) / NS_H)
            we.append((b - t0) / NS_H)
            ages += list((sel - t0) / NS_H)
            wid += [k] * len(sel)
            k += 1
    return np.array(ages, float), np.array(wid, int), np.array(ws, float), np.array(we, float)


def _nhpp_loglik(beta, t, S, T):
    n = len(t)
    denom = (T**beta - S**beta).sum()
    lam = n / denom
    return n * np.log(lam) + n * np.log(beta) + (beta - 1) * np.log(t).sum() - n, lam


def crow_amsaa(fail: pd.DataFrame, expo: pd.DataFrame, origin=None, conf: float = 0.9) -> CrowAMSAA:
    """Power-law NHPP over several turbines and observation windows (maximum likelihood)."""
    t, _, S, T = nhpp_windows(fail, expo, origin)
    n = len(t)
    hours = float((T - S).sum())
    if n < 3:
        return CrowAMSAA(np.nan, np.nan, np.nan, np.nan, n, expo["turbine_id"].nunique(), np.nan, np.nan, np.nan,
                         hours)
    # ages in units of the oldest window end: beta does not depend on the unit and T**beta stays finite
    scale = T.max()
    t, S, T = np.maximum(t, 1e-6) / scale, S / scale, T / scale
    lt = np.log(t)
    lS = np.where(S > 0, np.log(np.maximum(S, 1e-300)), 0.0)
    lT = np.log(T)

    def score(beta):
        Tb, Sb = T**beta, np.where(S > 0, S**beta, 0.0)
        return n / beta + lt.sum() - n * (Tb * lT - Sb * lS).sum() / (Tb - Sb).sum()

    beta = _root(score, 1e-4, 20.0)
    u, p_lap = laplace_observed(fail, expo)
    if not np.isfinite(beta):
        log.warning("Crow-AMSAA fit did not converge (%d failures)", n)
        return CrowAMSAA(np.nan, np.nan, np.nan, np.nan, n, expo["turbine_id"].nunique(), np.nan, u, p_lap, hours)
    lmax, lam = _nhpp_loglik(beta, t, S, T)
    cut = stats.chi2.ppf(conf, 1) / 2

    def f(b):
        return lmax - _nhpp_loglik(b, t, S, T)[0] - cut

    lo = _root(f, 1e-4, beta)
    hi = _root(f, beta, 20.0)
    p_lr = float(stats.chi2.sf(2 * (lmax - _nhpp_loglik(1.0, t, S, T)[0]), 1))
    lam = lam / scale**beta  # back to hours
    return CrowAMSAA(float(beta), float(lam), float(lo), float(hi), n, expo["turbine_id"].nunique(), p_lr, u,
                     p_lap, hours)


def laplace_observed(fail: pd.DataFrame, expo: pd.DataFrame) -> tuple[float, float]:
    """Laplace trend test on each turbine's observed-time clock (its windows joined end to end).

    Gaps then shorten the clock instead of splitting it, so the test looks at the long-term trend and not at
    what happens around each gap (failures often bunch up right before or after an outage of the data link).
    """
    taus, spans = [], []
    for tid, w in expo.sort_values("start").groupby("turbine_id"):
        a, b = _ns(w["start"]), _ns(w["end"])
        cum = np.r_[0, np.cumsum(b - a)]
        f = _ns(fail.loc[fail["turbine_id"] == tid, "start"])
        k = np.searchsorted(a, f, side="right") - 1
        ok = (k >= 0) & (f < b[np.clip(k, 0, None)])
        taus += list((cum[k[ok]] + f[ok] - a[k[ok]]) / NS_H)
        spans += [cum[-1] / NS_H] * int(ok.sum())
    return laplace_test(np.array(taus), np.zeros(len(taus)), np.array(spans))


def laplace_test(t, S, T) -> tuple[float, float]:
    """Laplace trend statistic for event times ``t`` each observed in its own window (S, T).

    Under a constant rate every event is uniform in its window; U > 0 means events cluster late (worsening).
    Two-sided p-value.
    """
    t, S, T = (np.asarray(x, float) for x in (t, S, T))
    if len(t) == 0:
        return np.nan, np.nan
    L = T - S
    u = ((t - (S + L / 2)).sum()) / np.sqrt((L**2).sum() / 12)
    return float(u), float(2 * stats.norm.sf(abs(u)))


def duane_fit(points: pd.DataFrame, total_hours: float) -> dict:
    """Power law on the fleet's cumulative time (one system, time-truncated): beta, lambda and an interval."""
    t = points["fleet_hours"].to_numpy(float)
    t = t[t > 0]
    n = len(t)
    if n < 3:
        return {"beta": np.nan, "lam": np.nan, "beta_lo": np.nan, "beta_hi": np.nan}
    beta = n / np.log(total_hours / t).sum()
    lo, hi = (beta * stats.chi2.ppf(q, 2 * n) / (2 * n) for q in (0.05, 0.95))
    return {"beta": float(beta), "lam": float(n / total_hours**beta), "beta_lo": float(lo), "beta_hi": float(hi)}


def duane_points(fail: pd.DataFrame, expo: pd.DataFrame) -> pd.DataFrame:
    """Fleet cumulative failures against cumulative fleet observation hours, in calendar order."""
    e = expo.copy()
    grid = np.sort(np.unique(np.r_[_ns(e["start"]), _ns(e["end"]), _ns(fail["start"])]))
    active = np.zeros(len(grid) - 1)
    for a, b in zip(_ns(e["start"]), _ns(e["end"])):
        i0, i1 = np.searchsorted(grid, [a, b])
        active[i0:i1] += 1
    cum_h = np.r_[0, np.cumsum(active * np.diff(grid) / NS_H)]
    fs = np.sort(_ns(fail["start"]))
    return pd.DataFrame({"time": pd.to_datetime(fs, utc=True), "fleet_hours": cum_h[np.searchsorted(grid, fs)],
                         "cum_failures": np.arange(1, len(fs) + 1)})


# ------------------------------------------------------------------------------------------- bad actors

def bh(p) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values."""
    p = np.asarray(p, float)
    n = len(p)
    if n == 0:
        return p
    o = np.argsort(p)
    q = p[o] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(n)
    out[o] = np.minimum(q, 1)
    return out


def turbine_outliers(fail: pd.DataFrame, up: pd.DataFrame, alpha: float = 0.05) -> pd.DataFrame:
    """Turbines with more failures than the fleet rate explains (one-sided exact Poisson test, BH-adjusted)."""
    h = up.groupby("turbine_id")["hours"].sum()
    n = fail.groupby("turbine_id").size().reindex(h.index, fill_value=0)
    rate = n.sum() / h.sum() if h.sum() else np.nan
    exp_n = rate * h
    p = stats.poisson.sf(n - 1, exp_n)
    dt = fail.groupby("turbine_id")["duration_h"].sum().reindex(h.index, fill_value=0)
    out = pd.DataFrame({"turbine_id": h.index, "failures": n.to_numpy(), "expected": exp_n.to_numpy(),
                        "ratio": (n / exp_n).to_numpy(), "p": p, "q": bh(p),
                        "failure_downtime_h": dt.to_numpy(),
                        "downtime_vs_median": (dt / dt.median()).to_numpy() if dt.median() > 0 else np.nan})
    out["bad_actor"] = (out["q"] < alpha) & (out["ratio"] > 1)
    return out.sort_values("ratio", ascending=False).reset_index(drop=True)


def code_turbine_outliers(fail: pd.DataFrame, up: pd.DataFrame, key: str = "code", alpha: float = 0.05,
                          min_n: int = 3) -> pd.DataFrame:
    """(code, turbine) pairs where one turbine has far more of a code than its share of operating hours."""
    h = up.groupby("turbine_id")["hours"].sum()
    share = h / h.sum()
    cnt = fail.groupby([key, "turbine_id"]).size().unstack(fill_value=0).reindex(columns=h.index, fill_value=0)
    rows = []
    for code, r in cnt.iterrows():
        tot = r.sum()
        for tid, k in r.items():
            if k == 0:
                continue
            e = tot * share[tid]
            rows.append((code, tid, int(k), int(tot), e, stats.poisson.sf(k - 1, e)))
    out = pd.DataFrame(rows, columns=[key, "turbine_id", "failures", "fleet_failures", "expected", "p"])
    out["q"] = bh(out["p"].to_numpy())
    out["ratio"] = out["failures"] / out["expected"]
    out["bad_actor"] = (out["q"] < alpha) & (out["failures"] >= min_n) & (out["ratio"] > 1)
    return out.sort_values(["bad_actor", "q"], ascending=[False, True]).reset_index(drop=True)


def jackknife(fail: pd.DataFrame, key: str = "code") -> pd.DataFrame:
    """Knights' jack-knife diagram: failures and mean restore time per ``key`` with the quadrant limits.

    Limits: mean failures per item (total failures / number of items) and the overall MTTR (total restore
    hours / total failures). Above both: acute + chronic; long repairs only: acute; frequent only: chronic.
    """
    g = fail.groupby(key)["duration_h"].agg(failures="size", mttr_h="mean", downtime_h="sum").reset_index()
    if g.empty:
        return g
    n_lim = g["failures"].sum() / len(g)
    t_lim = g["downtime_h"].sum() / g["failures"].sum()
    hi_n, hi_t = g["failures"] > n_lim, g["mttr_h"] > t_lim
    g["quadrant"] = np.select([hi_n & hi_t, hi_t, hi_n], ["acute+chronic", "acute", "chronic"], "ok")
    g["n_limit"], g["mttr_limit_h"] = n_lim, t_lim
    return g.sort_values("downtime_h", ascending=False).reset_index(drop=True)
