"""Reliability indicators from failure episodes: MTBF / MTTR, repair-time distributions, Weibull, Crow-AMSAA and
bad actors.

Definitions used throughout:

* **Failure** (in the tables; "forced-outage stop episode" in the reports) - a stop episode in which a stop of a
  failure category (``schema.FAILURE_CATEGORIES``, forced outage by default) was at some moment the reason the
  turbine stood still, not flagged fleet-wide (a common cause such as a grid loss is not a turbine failure).
  The count depends on the merge gap of the episodes and on what counts as a common cause; ``study.sensitivity``
  shows by how much.
* **Repeat chains** - consecutive failures of one code on one turbine within the repeat window
  (``compound.repeats``). ``decluster`` keeps the first failure of each chain: the trend and the bad-actor tests
  run on chains, because repeats are not independent events.
* **Operating time** - observation time minus all stop time (failures cannot start while stopped).
* **MTBF** - operating hours / failures, with a two-sided chi-square interval for a time-truncated test (it
  assumes independent failures; ``mtbf_bootstrap`` gives the interval that does not).
* **MTTR** - mean time down for the fault: the attributed hours of the episode's failure-category stops
  (``fault_h``), waiting, travel and logistics included; maintenance done in the same stop after the fault and
  uncategorised stops are left out (they are in the episode duration).
* **Weibull** - fitted by maximum likelihood to the operating times between failures of each turbine (a renewal
  view); the last interval of every observation window is right-censored, the first is dropped (its start is
  unknown). Shape < 1: early failures / repeats, ~1: random, > 1: wear-out.
* **Crow-AMSAA** - the power-law non-homogeneous Poisson process, intensity
  ``lambda * beta * t**(beta - 1)`` in turbine age ``t``, fitted by maximum likelihood over all turbines and all
  their observation windows (gaps and late starts handled exactly). ``beta < 1``: failures thin out
  (reliability improves), ``beta > 1``: they become more frequent. The verdict comes from the likelihood-ratio
  test of beta = 1; the Laplace test on each turbine's observed-time clock is a model-free second opinion
  (it does not depend on the age origin). ``trend`` fits the de-clustered failures and resamples whole
  turbines: the verdict needs the bootstrap interval of beta to lie on one side of 1.
* **Bad actors** - turbines (and code x turbine pairs) with more repeat chains than the fleet rate explains
  (exact Poisson test, Benjamini-Hochberg false discovery rate), and the jack-knife diagram of Knights (2001):
  frequency against mean time down, with acute, chronic and acute + chronic quadrants.

Usage:
    fail = failures(ep)
    up = uptime(ep, expo)
    kpi = mtbf_mttr(fail, up, by=["turbine_id"])
    w = weibull_fit(*tbf(fail, up))
    ca = crow_amsaa(fail, expo, origin={"T1": cod, ...})
    bad = turbine_outliers(fail, up)

Operating time, MTBF/MTTR, Crow-AMSAA and the Laplace test are computed by the res-asset-kpi library
(``res_asset_kpi.wind``); this module adapts them to the tables of this package and adds the rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from res_asset_kpi import wind
from res_asset_kpi.wind import HOURS_PER_YEAR, crow_amsaa, laplace_observed, laplace_test  # noqa: F401
from scipy import optimize, stats

from wind_om.events import NS_H, _ns

log = logging.getLogger(__name__)


def failures(ep: pd.DataFrame, exclude_fleet_wide: bool = True, min_duration_h: float = 0.0) -> pd.DataFrame:
    """Failure episodes (see module docstring and ``events.episodes``)."""
    m = ep["failure"].astype(bool) & (ep["duration_h"] >= min_duration_h)
    if exclude_fleet_wide and "fleet_wide" in ep:
        m &= ~ep["fleet_wide"]
    return ep[m].sort_values(["turbine_id", "start"]).reset_index(drop=True)


def uptime(ep: pd.DataFrame, expo: pd.DataFrame) -> pd.DataFrame:
    """Operating intervals per turbine: observation windows minus all stop episodes (turbine_id, start, end)."""
    return wind.operating_time(expo, ep)[["turbine_id", "window", "start", "end", "hours"]]


def restore_h(fail: pd.DataFrame) -> pd.Series:
    """Time down for the fault per failure: ``fault_h`` when the episodes carry it, else the episode duration."""
    return fail["fault_h"] if "fault_h" in fail else fail["duration_h"]


def mtbf_mttr(fail: pd.DataFrame, up: pd.DataFrame, by=("turbine_id",), conf: float = 0.9) -> pd.DataFrame:
    """Failures, operating hours, MTBF (with interval), failure rate and restore-time statistics per group.

    Restore times are the fault times (``restore_h``); ``episode_h`` is the mean episode duration.
    Grouping by ``component`` (or anything not tied to a turbine) uses the operating hours of the whole fleet,
    since every turbine carries every component.
    """
    by = list(by)
    out = wind.mtbf_mttr(fail.assign(duration_h=restore_h(fail)), up.groupby("turbine_id")["hours"].sum(),
                         by=by, conf=conf)
    if len(fail) and len(out):
        out = out.merge(fail.groupby(by)["duration_h"].mean().rename("episode_h").reset_index(), on=by, how="left")
    return out.rename(columns={"failures_per_unit_year": "failures_per_turbine_year"})


def decluster(rep: pd.DataFrame) -> pd.DataFrame:
    """The first failure of every repeat chain (``compound.repeats`` output); other tables pass through."""
    return rep[rep["chain_pos"] == 0] if "chain_pos" in rep else rep


def turbine_bootstrap(fail: pd.DataFrame, expo: pd.DataFrame, stat, n_boot: int = 500, seed: int = 0,
                      conf: float = 0.9, origin=None) -> tuple[float, float]:
    """Percentile interval of ``stat(fail, expo, origin)`` over fleets resampled turbine by turbine.

    Failures of one turbine are not independent (repeats, a weak turbine); resampling whole turbines keeps that
    dependence, so the interval is honest where the analytic ones (chi-square, profile likelihood) are too narrow.
    """
    rng = np.random.default_rng(seed)
    tids = np.array(sorted(expo["turbine_id"].unique()))
    f_by = dict(tuple(fail.groupby("turbine_id")))
    e_by = dict(tuple(expo.groupby("turbine_id")))
    empty = fail.iloc[:0]
    vals = []
    for _ in range(n_boot):
        pick = rng.choice(tids, len(tids))
        ff = pd.concat([f_by.get(t, empty).assign(turbine_id=f"b{k}") for k, t in enumerate(pick)])
        ee = pd.concat([e_by[t].assign(turbine_id=f"b{k}") for k, t in enumerate(pick)])
        org = {f"b{k}": origin[t] for k, t in enumerate(pick) if t in origin} if origin else None
        vals.append(stat(ff, ee, org))
    a = (1 - conf) / 2
    lo, hi = np.nanpercentile(np.asarray(vals, float), [100 * a, 100 * (1 - a)])
    return float(lo), float(hi)


def trend(rep: pd.DataFrame, expo: pd.DataFrame, origin=None, n_boot: int = 500, seed: int = 0) -> dict:
    """Crow-AMSAA on all failures and on de-clustered failures, with a turbine-bootstrap interval of the latter.

    Verdict: ``improving`` / ``deteriorating`` when the 90 % bootstrap interval of the de-clustered beta lies
    below / above 1, else ``no clear trend``.
    """
    raw = crow_amsaa(rep, expo, origin)
    dec_f = decluster(rep)
    dec = crow_amsaa(dec_f, expo, origin)

    def beta(f, e, o):
        return crow_amsaa(f, e, o).beta if len(f) >= 5 else np.nan

    enough = len(dec_f) >= 5
    lo, hi = turbine_bootstrap(dec_f, expo, beta, n_boot, seed, origin=origin) if enough else (np.nan, np.nan)
    verdict = ("too few failures" if not enough else "improving" if hi < 1 else
               "deteriorating" if lo > 1 else "no clear trend")
    return {"beta_all": raw.beta, "beta_all_lo": raw.beta_lo, "beta_all_hi": raw.beta_hi, "p_all": raw.p_trend,
            "failures_all": raw.failures, "beta": dec.beta, "beta_lo": dec.beta_lo, "beta_hi": dec.beta_hi,
            "p_trend": dec.p_trend, "p_laplace": dec.p_laplace, "failures": dec.failures,
            "beta_boot_lo": lo, "beta_boot_hi": hi, "verdict": verdict}


def mtbf_bootstrap(fail: pd.DataFrame, up: pd.DataFrame, n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
    """Turbine-bootstrap 90 % interval of the fleet MTBF (operating hours / failures)."""
    h = up.groupby("turbine_id")["hours"].sum()
    n = fail.groupby("turbine_id").size().reindex(h.index, fill_value=0)
    k = np.random.default_rng(seed).integers(0, len(h), size=(n_boot, len(h)))
    hs, ns = h.to_numpy()[k].sum(1), n.to_numpy()[k].sum(1)
    m = np.where(ns > 0, hs / np.maximum(ns, 1), np.nan)
    lo, hi = np.nanpercentile(m, [5, 95])
    return float(lo), float(hi)


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

def crow_amsaa_row(ca: wind.CrowAMSAA) -> dict:
    """A Crow-AMSAA fit (``crow_amsaa``) as a row of the published tables."""
    return {"crow_beta": ca.beta, "crow_beta_lo": ca.beta_lo, "crow_beta_hi": ca.beta_hi, "crow_lambda": ca.lam,
            "failures": ca.failures, "turbines": ca.units, "observed_turbine_years": ca.hours / HOURS_PER_YEAR,
            "p_trend_lr": ca.p_trend, "laplace_u": ca.laplace_u, "p_laplace": ca.p_laplace, "verdict": ca.verdict,
            "laplace_agrees": ca.laplace_agrees}


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
    """Turbines with more failures than the fleet rate explains (one-sided exact Poisson test, BH-adjusted).

    With the repeat columns of ``compound.repeats`` the test counts repeat chains (``chains``), not every
    failure: a reset without a fix repeats the same event, and counting each repeat would make the Poisson test
    far too confident (turbine-year counts of failures are strongly over-dispersed).
    """
    h = up.groupby("turbine_id")["hours"].sum()
    n_all = fail.groupby("turbine_id").size().reindex(h.index, fill_value=0)
    n = decluster(fail).groupby("turbine_id").size().reindex(h.index, fill_value=0)
    rate = n.sum() / h.sum() if h.sum() else np.nan
    exp_n = rate * h
    p = stats.poisson.sf(n - 1, exp_n)
    dt = restore_h(fail).groupby(fail["turbine_id"]).sum().reindex(h.index, fill_value=0)
    out = pd.DataFrame({"turbine_id": h.index, "failures": n_all.to_numpy(), "chains": n.to_numpy(),
                        "expected": exp_n.to_numpy(), "ratio": (n / exp_n).to_numpy(), "p": p, "q": bh(p),
                        "failure_downtime_h": dt.to_numpy(),
                        "downtime_vs_median": (dt / dt.median()).to_numpy() if dt.median() > 0 else np.nan})
    out["bad_actor"] = (out["q"] < alpha) & (out["ratio"] > 1)
    return out.sort_values("ratio", ascending=False).reset_index(drop=True)


def code_turbine_outliers(fail: pd.DataFrame, up: pd.DataFrame, key: str = "code", alpha: float = 0.05,
                          min_n: int = 3) -> pd.DataFrame:
    """(code, turbine) pairs where one turbine has far more of a code than its share of operating hours.

    Counts repeat chains when ``fail`` has the repeat columns (see ``turbine_outliers``).
    """
    h = up.groupby("turbine_id")["hours"].sum()
    share = h / h.sum()
    cnt = (decluster(fail).groupby([key, "turbine_id"]).size().unstack(fill_value=0)
           .reindex(columns=h.index, fill_value=0))
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

    Limits: mean failures per item (total failures / number of items) and the overall MTTR (total fault
    hours / total failures). Above both: acute + chronic; long repairs only: acute; frequent only: chronic.
    """
    g = restore_h(fail).groupby(fail[key]).agg(failures="size", mttr_h="mean", downtime_h="sum").reset_index()
    if g.empty:
        return g
    n_lim = g["failures"].sum() / len(g)
    t_lim = g["downtime_h"].sum() / g["failures"].sum()
    hi_n, hi_t = g["failures"] > n_lim, g["mttr_h"] > t_lim
    g["quadrant"] = np.select([hi_n & hi_t, hi_t, hi_n], ["acute+chronic", "acute", "chronic"], "ok")
    g["n_limit"], g["mttr_limit_h"] = n_lim, t_lim
    return g.sort_values("downtime_h", ascending=False).reset_index(drop=True)
