"""The estimators recover known parameters from synthetic logs."""
import numpy as np
import pytest

from wind_om import compound
from wind_om import reliability as R
from wind_om.analysis import analyse
from wind_om.synthetic import mapping_frame, synthetic_log


@pytest.fixture(scope="module")
def hpp():
    log, truth = synthetic_log(n_turbines=30, years=5, process="hpp", mtbf_h=600, ttr_median_h=6, ttr_sigma=1.0,
                               seed=3)
    return analyse(log, mapping_frame()), truth


def test_mtbf_mttr_recovered(hpp):
    a, truth = hpp
    s = a.summary()
    assert s["mtbf_h"] == pytest.approx(truth.mtbf_h, rel=0.05)
    assert s["mttr_h"] == pytest.approx(truth.ttr_mean_h, rel=0.08)
    fleet = R.mtbf_mttr(a.fail.assign(fleet="all"), a.up, by=["fleet"]).iloc[0]
    assert fleet["mtbf_lo_h"] < truth.mtbf_h < fleet["mtbf_hi_h"]
    ln = R.lognormal_fit(a.fail["duration_h"])
    assert ln["median_h"] == pytest.approx(truth.ttr_median_h, rel=0.06)
    assert ln["sigma"] == pytest.approx(truth.ttr_sigma, abs=0.06)


def test_component_rates_add_up(hpp):
    a, truth = hpp
    comp = R.mtbf_mttr(a.fail, a.up, by=["component"])
    # component failure rates per turbine-year add up to the fleet rate
    total = comp["failures_per_turbine_year"].sum()
    assert total == pytest.approx(8766 / truth.mtbf_h, rel=0.06)
    # the pitch code carries 30 % of the failures
    pitch = comp.set_index("component").loc["pitch", "failures"]
    assert pitch / comp["failures"].sum() == pytest.approx(0.30, abs=0.03)


def test_hpp_has_no_trend(hpp):
    a, truth = hpp
    ca = R.crow_amsaa(a.fail, a.expo, truth.origin)
    assert ca.beta_lo < 1.0 < ca.beta_hi + 0.01
    assert ca.p_laplace > 0.05 and ca.verdict == "no significant trend"
    w = R.weibull_fit(*R.tbf(a.fail, a.up))
    assert w.shape == pytest.approx(1.0, abs=0.05)
    assert w.scale_h == pytest.approx(truth.mtbf_h, rel=0.06)


@pytest.mark.parametrize("beta", [0.6, 1.4])
def test_crow_amsaa_beta_recovered_mid_life(beta):
    # observation starts at age 2 years: the windowed likelihood must not assume age 0 at the first record
    log, truth = synthetic_log(n_turbines=30, years=6, process="power_law", beta=beta, mtbf_h=500,
                               age_at_start_h=2 * 8766, seed=5)
    a = analyse(log, mapping_frame())
    ca = R.crow_amsaa(a.fail, a.expo, truth.origin)
    assert ca.beta == pytest.approx(beta, abs=0.06)
    assert ca.beta_lo < beta < ca.beta_hi
    assert ca.verdict == ("improving" if beta < 1 else "deteriorating")


def test_crow_amsaa_with_gaps():
    # 30-day logging gaps on several turbines: failures there are unseen, exposure must exclude them
    log, truth = synthetic_log(n_turbines=24, years=6, process="power_law", beta=1.4, mtbf_h=400, gaps=12,
                               seed=11)
    a = analyse(log, mapping_frame(), start=truth.window[0], end=truth.window[1])
    assert len(a.expo) == 24 + 12  # each gap splits one window in two
    ca = R.crow_amsaa(a.fail, a.expo, truth.origin)
    assert ca.beta == pytest.approx(1.4, abs=0.08)
    s = a.summary()
    lost_h = sum((ge - gs).total_seconds() / 3600 for _, gs, ge in truth.gaps)
    assert s["observed_turbine_years"] * 8766 == pytest.approx(24 * 6 * 8766 - lost_h, rel=0.002)


@pytest.mark.parametrize("shape", [0.7, 2.0])
def test_weibull_shape_recovered(shape):
    log, _ = synthetic_log(n_turbines=40, years=5, process="weibull", shape=shape, scale_h=500,
                               ttr_median_h=2, seed=7)
    a = analyse(log, mapping_frame())
    w = R.weibull_fit(*R.tbf(a.fail, a.up))
    assert w.shape == pytest.approx(shape, rel=0.05)
    assert w.scale_h == pytest.approx(500, rel=0.06)
    assert w.n_censored == 40


def test_weibull_mle_with_heavy_censoring():
    rng = np.random.default_rng(2)
    t = 300 * rng.weibull(1.6, 4000)
    c = rng.uniform(0, 500, 4000)  # random right censoring of about half the sample
    w = R.weibull_fit(np.minimum(t, c), t <= c)
    assert w.shape == pytest.approx(1.6, rel=0.05)
    assert w.scale_h == pytest.approx(300, rel=0.04)


def test_bad_actor_found():
    log, truth = synthetic_log(n_turbines=15, years=3, process="hpp", mtbf_h=700, bad_factor=2.5, seed=4)
    a = analyse(log, mapping_frame())
    bad = R.turbine_outliers(a.fail, a.up)
    assert bad.loc[bad["bad_actor"], "turbine_id"].tolist() == [truth.bad_turbine]
    jk = R.jackknife(a.fail, key="turbine_id").set_index("turbine_id")
    assert jk.loc[truth.bad_turbine, "quadrant"] in ("chronic", "acute+chronic")
    pairs = R.code_turbine_outliers(a.fail, a.up)
    assert set(pairs.loc[pairs["bad_actor"], "turbine_id"]) <= {truth.bad_turbine}


def test_repeats_above_chance():
    base, _ = synthetic_log(n_turbines=20, years=3, process="hpp", mtbf_h=400, seed=8)
    rep, truth = synthetic_log(n_turbines=20, years=3, process="hpp", mtbf_h=400, repeat_prob=0.3,
                               repeat_days=2, seed=8)
    s0 = compound.repeat_summary((a0 := analyse(base, mapping_frame())).rep, operating_h=a0.up["hours"].sum())
    s1 = compound.repeat_summary((a1 := analyse(rep, mapping_frame())).rep, operating_h=a1.up["hours"].sum())
    # without injected repeats the observed rate matches the Poisson chance rate
    assert np.average(s0["excess"], weights=s0["failures"]) == pytest.approx(0.0, abs=0.02)
    # 30 % of failures come back within 2 days: about 0.3 / 1.3 of all failures are repeats
    assert np.average(s1["excess"], weights=s1["failures"]) > 0.15
    flagged = a1.rep.set_index(["turbine_id", "start"])["is_repeat"]
    hits = [bool(flagged.get(k, False)) for k in truth.repeat_starts]
    assert np.mean(hits) > 0.97


def test_spares_poisson():
    from wind_om.spares import spares

    log, _ = synthetic_log(n_turbines=20, years=4, process="hpp", mtbf_h=800, seed=9)
    a = analyse(log, mapping_frame())
    sp = spares(a.fail, a.up, n_turbines=20, lead_time_months=3, service_level=0.95).set_index("component")
    lam = 8766 / 800 * 0.30 * 20  # pitch failures per year for the fleet
    assert sp.loc["pitch", "expected_per_year_fleet"] == pytest.approx(lam, rel=0.1)
    assert sp.loc["pitch", "p_stockout"] <= 0.05
