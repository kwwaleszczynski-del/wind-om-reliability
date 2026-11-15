"""Clustered failures, common causes and code-dictionary coverage: what the headline numbers depend on."""
import numpy as np
import pandas as pd
import pytest

from wind_om import events
from wind_om import reliability as R
from wind_om.analysis import Settings, analyse
from wind_om.schema import validate
from wind_om.study import dictionary_coverage, sensitivity
from wind_om.synthetic import mapping_frame, synthetic_log


def T(s):
    return pd.Timestamp(s, tz="UTC")


def test_coincident_flag():
    f = pd.DataFrame({"turbine_id": ["A", "B", "A", "C"],
                      "start": [T("2024-01-01 10:00"), T("2024-01-01 10:08"), T("2024-01-01 10:09"),
                                T("2024-01-01 12:00")]})
    # A at 10:00 and B at 10:08 are within 10 minutes; A at 10:09 is too (B); C alone
    assert events.flag_coincident(f).tolist() == [True, True, True, False]


@pytest.fixture(scope="module")
def clustered():
    # constant failure rate (no trend) but half of the failures come back within 2 days: clustered events
    log, truth = synthetic_log(n_turbines=20, years=5, process="hpp", mtbf_h=500, repeat_prob=0.5, repeat_days=2,
                               seed=21)
    return analyse(log, mapping_frame(), start=truth.window[0], end=truth.window[1]), truth


def test_trend_on_clustered_failures(clustered):
    a, truth = clustered
    tr = R.trend(a.rep, a.expo, truth.origin, n_boot=200)
    # repeat chains carry the trend information; the bootstrap interval resamples whole turbines
    assert tr["failures"] < tr["failures_all"]
    assert tr["beta"] == pytest.approx(1.0, abs=0.08)
    assert tr["beta_boot_lo"] < 1.0 < tr["beta_boot_hi"]
    assert tr["verdict"] == "no clear trend"
    # on every event, repeats included, the independence-based profile interval is too narrow: resampling whole
    # turbines (which keeps each chain together) gives a clearly wider one
    lo, hi = R.turbine_bootstrap(a.rep, a.expo, lambda f, e, o: R.crow_amsaa(f, e, o).beta, 200, origin=truth.origin)
    assert hi - lo > 1.1 * (tr["beta_all_hi"] - tr["beta_all_lo"])
    lo, hi = R.mtbf_bootstrap(a.fail, a.up)
    assert lo < a.summary()["mtbf_h"] < hi


def test_power_law_trend_found_on_chains():
    log, truth = synthetic_log(n_turbines=30, years=6, process="power_law", beta=1.4, mtbf_h=400, seed=5)
    a = analyse(log, mapping_frame(), start=truth.window[0], end=truth.window[1])
    tr = R.trend(a.rep, a.expo, truth.origin, n_boot=200)
    assert tr["verdict"] == "deteriorating" and tr["beta_boot_lo"] > 1


def test_bad_actor_made_of_repeats_is_not_flagged():
    log, _ = synthetic_log(n_turbines=12, years=3, process="hpp", mtbf_h=600, seed=13)
    a0 = analyse(log, mapping_frame())
    # turbine WTG01 repeats every one of its failures twice, one and two days after the restart
    f1 = log[(log["turbine_id"] == "WTG01") & log["stop"]]
    extra = [f1.assign(start=f1["end"] + pd.Timedelta(days=d), end=f1["end"] + pd.Timedelta(days=d, hours=2))
             for d in (1, 2)]
    a = analyse(validate(pd.concat([log, *extra], ignore_index=True)), mapping_frame(), start=a0.start, end=a0.end)
    raw = R.turbine_outliers(a.fail, a.up)
    chains = R.turbine_outliers(a.rep, a.up)
    assert raw.loc[raw["bad_actor"], "turbine_id"].tolist() == ["WTG01"]
    assert not chains["bad_actor"].any()
    assert chains.set_index("turbine_id").loc["WTG01", "failures"] > 2.5 * chains.set_index(
        "turbine_id").loc["WTG01", "chains"]


def test_sensitivity_table():
    log, truth = synthetic_log(n_turbines=10, years=2, process="hpp", mtbf_h=300, repeat_prob=0.3, repeat_days=0.5,
                               seed=17)
    s = sensitivity(log, mapping_frame(), truth.window[0], truth.window[1], truth.origin, Settings(),
                    exclude=("without code 1001", ("1001",)))
    n = s.set_index("variant")["failures"]
    assert n.iloc[0] >= n["merge gap 1h"] >= n["merge gap 24h"]
    assert n["repeat chains only (first failure of each chain)"] < n.iloc[0]
    assert n["without code 1001"] < n.iloc[0]
    assert s["vs_published"].iloc[0] == 0


def test_dictionary_coverage_flags_unknown_codes():
    log, truth = synthetic_log(n_turbines=5, years=2, process="hpp", mtbf_h=200, start="2024-01-01", seed=3)
    late = log[log["start"] >= T("2025-01-01")].copy()
    # a code the mapping has never seen, only in the second year, in a log without its own category or stop flag
    # (like the Hill of Towie alarm log, where both come from the code dictionary)
    late["code"], late["category"], late["stop"] = "99999", "unknown", pd.NA
    a = analyse(validate(pd.concat([log[log["start"] < T("2025-01-01")], late], ignore_index=True)),
                mapping_frame(), start=truth.window[0], end=truth.window[1])
    cov = dictionary_coverage(a).set_index("year")
    assert cov.loc[2024, "unmapped_share"] == 0
    assert cov.loc[2025, "unmapped_share"] > 0.9
    # the unknown code's stops are not failures: the second year shows almost none
    years = a.fail["start"].dt.year.value_counts()
    assert years.get(2025, 0) < 0.05 * years[2024]
    assert np.isfinite(cov["active_h"]).all()
