"""Expected failures per component and year, and an illustrative spare-parts stock from the Poisson law.

For component c with n_c failures in T operating turbine-years, the rate is n_c / T per turbine and year. For
a fleet of N turbines and a replenishment lead time L (months), the demand during one lead time is Poisson
with mean ``rate * N * L / 12 * part_share``, where ``part_share`` is the share of failures that consume a part
(1.0 = every failure, an upper bound - the logs do not say which repairs used a part). The stock is the
smallest number of parts that covers the lead-time demand with probability ``service_level``.

This is a first-order illustration: it assumes a constant failure rate (check the Crow-AMSAA trend first),
one part per failure and independent failures. Real stocking decisions also weigh part cost, OEM pool
agreements and contract terms (to be checked against the actual contract).

Usage:
    sp = spares(fail, up, n_turbines=14, lead_time_months=3, service_level=0.95)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from wind_om.reliability import HOURS_PER_YEAR


def spares(fail: pd.DataFrame, up: pd.DataFrame, n_turbines: int, lead_time_months: float = 3.0,
           service_level: float = 0.95, part_share: float | dict = 1.0, key: str = "component") -> pd.DataFrame:
    years = up["hours"].sum() / HOURS_PER_YEAR
    n = fail.groupby(key).size()
    out = pd.DataFrame({"failures": n, "rate_per_turbine_year": n / years})
    out["expected_per_year_fleet"] = out["rate_per_turbine_year"] * n_turbines
    share = out.index.map(lambda c: part_share.get(c, 1.0) if isinstance(part_share, dict) else part_share)
    out["part_share"] = np.asarray(share, dtype=float)
    out["lead_time_demand_mean"] = out["expected_per_year_fleet"] * lead_time_months / 12 * out["part_share"]
    out["stock"] = stats.poisson.ppf(service_level, out["lead_time_demand_mean"]).astype(int)
    out["p_stockout"] = stats.poisson.sf(out["stock"], out["lead_time_demand_mean"])
    return out.sort_values("expected_per_year_fleet", ascending=False).rename_axis(key).reset_index()
