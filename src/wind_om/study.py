"""The study on the three open farms, the full-period tables, the demo and the export of an open farm.

``run_study`` per farm: canonical log -> (Hill of Towie: code catalogue inferred from SCADA) -> analysis over
the whole log -> lost energy, log-vs-SCADA agreement and clock check for the years with SCADA -> tables in
``results/<farm>/``, one KPI pack per farm in ``docs/examples/`` and the dashboard data ``docs/data.js``.

Usage:
    python -m wind_om study --scada-dir ../wind-downtime-availability/data
    tables, summary = full_tables(analysis)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from wind_om import compound
from wind_om import reliability as R
from wind_om.analysis import Analysis, Settings, analyse
from wind_om.mapping import read_mapping
from wind_om.pareto import pareto, vital_few
from wind_om.spares import spares

log = logging.getLogger(__name__)
SCADA_YEARS = {"kelmarsh": [2016, 2017, 2018, 2019, 2020, 2021], "penmanshiel": [2018, 2020],
               "hill_of_towie": [2021]}
CLOCK_YEAR = {"kelmarsh": 2020, "penmanshiel": 2020, "hill_of_towie": 2021}
KPI_MONTH = {"kelmarsh": "2021-03", "penmanshiel": "2020-11", "hill_of_towie": "2021-11"}
NAMES = {"kelmarsh": "Kelmarsh (6 x Senvion MM92)", "penmanshiel": "Penmanshiel (14 x Senvion MM82)",
         "hill_of_towie": "Hill of Towie (21 x Siemens SWT-2.3-VS-82)"}


def _years(a: Analysis) -> list[int]:
    return list(range(a.start.year, a.end.year + 1))


def yearly(a: Analysis) -> pd.DataFrame:
    """Failures, rates, restore times, stop hours by category and lost energy per calendar year (UTC)."""
    rows = []
    for y in _years(a):
        s, e = max(pd.Timestamp(f"{y}-01-01", tz="UTC"), a.start), min(pd.Timestamp(f"{y + 1}-01-01", tz="UTC"),
                                                                      a.end)
        if e <= s:
            continue
        up = a.up[(a.up["end"] > s) & (a.up["start"] < e)]
        op_h = float(np.clip(np.minimum(up["end"], e) - np.maximum(up["start"], s), pd.Timedelta(0), None).sum()
                     / pd.Timedelta("1h"))
        ex = a.expo
        obs_h = float(np.clip(np.minimum(ex["end"], e) - np.maximum(ex["start"], s), pd.Timedelta(0), None).sum()
                      / pd.Timedelta("1h"))
        f = a.fail[(a.fail["start"] >= s) & (a.fail["start"] < e)]
        rp = a.rep[(a.rep["start"] >= s) & (a.rep["start"] < e)]
        rs = a.res[(a.res["start"] >= s) & (a.res["start"] < e)]
        cat = pareto(a.ev, a.seg, a.ep, "category", s, e).set_index("category")
        row = {"year": y, "observed_turbine_years": obs_h / R.HOURS_PER_YEAR, "failures": len(f),
               "failures_per_turbine_year": len(f) / (obs_h / R.HOURS_PER_YEAR) if obs_h else np.nan,
               "mtbf_h": op_h / len(f) if len(f) else np.nan, "mttr_h": f["duration_h"].mean(),
               "ttr_median_h": f["duration_h"].median(), "failure_downtime_h": f["duration_h"].sum(),
               "repeat_rate": rp["is_repeat"].mean() if len(rp) else np.nan,
               "auto_reset_share": (rs["restoration"] == "auto_reset").mean() if len(rs) else np.nan,
               "on_site_share": (rs["restoration"] == "on_site").mean() if len(rs) else np.nan}
        for c in cat.index:
            row[f"stop_h_{c}"] = cat.loc[c, "hours"]
        if "lost_mwh" in cat:
            seen = a.seg.loc[(a.seg["start"] < e) & (a.seg["end"] > s), "lost_kwh"].notna().any()
            row["lost_mwh"] = cat["lost_mwh"].sum() if seen else np.nan
            row["lost_mwh_failures"] = cat.loc["forced_outage", "lost_mwh"] if seen and "forced_outage" in cat.index \
                else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def full_tables(a: Analysis) -> tuple[dict, dict]:
    """Every table of the whole analysis window, and a summary dict."""
    up_h = a.up["hours"].sum()
    t = {
        "pareto_codes": pareto(a.ev, a.seg, a.ep, "code"),
        "pareto_components": pareto(a.ev, a.seg, a.ep, "component"),
        "pareto_categories": pareto(a.ev, a.seg, a.ep, "category"),
        "mtbf_turbines": R.mtbf_mttr(a.fail, a.up, by=["turbine_id"]),
        "mtbf_components": R.mtbf_mttr(a.fail, a.up, by=["component"]),
        "bad_actor_turbines": R.turbine_outliers(a.fail, a.up),
        "bad_actor_code_turbine": R.code_turbine_outliers(a.fail, a.up),
        "jackknife_codes": R.jackknife(a.fail, "code"),
        "jackknife_components": R.jackknife(a.fail, "component"),
        "repeats": compound.repeat_summary(a.rep, operating_h=up_h, days=a.settings.repeat_days),
        "cascade_pairs": a.pairs.head(200),
        "restoration": compound.restoration_summary(a.res),
        "spares": spares(a.fail, a.up, n_turbines=len(a.turbines)),
        "yearly": yearly(a),
        "duane": R.duane_points(a.fail, a.expo),
    }
    ln = [{"component": c, **R.lognormal_fit(g["duration_h"])} for c, g in a.fail.groupby("component")]
    t["restore_lognormal"] = pd.DataFrame(ln).sort_values("n", ascending=False) if ln else pd.DataFrame()
    wb = [{"group": "all", **R.weibull_fit(*R.tbf(a.fail, a.up)).as_dict()}]
    for c, n in a.fail["component"].value_counts().items():
        if n >= 20:
            wb.append({"group": c, **R.weibull_fit(*R.tbf(a.fail, a.up, group=("component", c))).as_dict()})
    t["weibull"] = pd.DataFrame(wb)
    ca = [{"group": "fleet", **R.crow_amsaa(a.fail, a.expo, a.origin).as_dict()}]
    for tid in a.turbines:
        ca.append({"group": tid, **R.crow_amsaa(a.fail[a.fail["turbine_id"] == tid],
                                                a.expo[a.expo["turbine_id"] == tid], a.origin).as_dict()})
    for c, n in a.fail["component"].value_counts().items():
        if n >= 10:
            ca.append({"group": f"component:{c}", **R.crow_amsaa(a.fail[a.fail["component"] == c], a.expo,
                                                                 a.origin).as_dict()})
    t["crow_amsaa"] = pd.DataFrame(ca)
    fw = a.ep[a.ep["fleet_wide"]]
    t["fleet_wide"] = (fw.groupby("fleet_group").agg(start=("start", "min"), turbines=("turbine_id", "nunique"),
                                                      hours=("stop_h", "sum"), code=("code", lambda s: s.mode()[0]),
                                                      category=("category", lambda s: s.mode()[0]))
                       .reset_index(drop=True) if len(fw) else pd.DataFrame())
    s = a.summary()
    fleet = t["crow_amsaa"].iloc[0]
    s.update({"start": str(a.start), "end": str(a.end), "crow_beta": fleet["crow_beta"],
              "crow_beta_lo": fleet["crow_beta_lo"], "crow_beta_hi": fleet["crow_beta_hi"],
              "p_laplace": fleet["p_laplace"], "trend": fleet["verdict"],
              "weibull_shape": t["weibull"].iloc[0]["weibull_shape"],
              "weibull_shape_lo": t["weibull"].iloc[0]["weibull_shape_lo"],
              "weibull_shape_hi": t["weibull"].iloc[0]["weibull_shape_hi"],
              "codes_80pct_downtime": vital_few(t["pareto_codes"]),
              "codes_with_downtime": int((t["pareto_codes"]["hours"] > 0).sum()),
              "bad_actor_turbines": t["bad_actor_turbines"].loc[t["bad_actor_turbines"]["bad_actor"],
                                                                "turbine_id"].tolist(),
              "bad_actor_code_turbine_pairs": int(t["bad_actor_code_turbine"]["bad_actor"].sum()),
              "restoration_shares": dict(zip(t["restoration"]["restoration"], t["restoration"]["share"])),
              "response_median_h": t["restoration"].set_index("restoration")["response_median_h"].get("on_site"),
              "unmapped_downtime_share": float(t["pareto_components"].set_index("component")["share"].get(
                  "unmapped", 0.0))})
    if len(t["repeats"]):
        r = t["repeats"]
        s["repeat_rate_chance"] = float(np.average(r["chance_rate"], weights=r["failures"]))
    return t, s


# ------------------------------------------------------------------------------- Hill of Towie catalogue

HOT_PUBLISHED_CATEGORY = {"1005": "environmental", "8000": "environmental", "8210": "environmental",
                          "8230": "environmental", "8234": "environmental", "8235": "environmental",
                          "8236": "environmental", "102": "environmental", "10105": "technical_standby",
                          "3130": "technical_standby", "20": "information", "25": "information"}
HOT_PUBLISHED_COMPONENT = {"1005": "environment", "8000": "environment", "8210": "environment",
                           "8230": "environment", "8234": "environment", "8235": "environment",
                           "8236": "environment", "102": "environment", "10105": "yaw", "3130": "pitch",
                           "20": "generator", "25": "generator"}


def hot_catalogue(ev: pd.DataFrame, scada_dir: Path, data_dir: Path, year: int = 2021,
                  min_intervals: int = 30, stop_share: float = 0.7, error_share: float = 0.5) -> tuple[
                      pd.DataFrame, pd.DataFrame]:
    """Code catalogue of Hill of Towie inferred from one year of SCADA, and the mapping derived from it.

    Published codes keep their description and stopping flag. Other codes with at least ``min_intervals``
    active 10-minute intervals become stopping when SCADA shows the turbine stopped in ``stop_share`` of them,
    and forced outages when the controller's documented "turbine error active" timer runs during at least
    ``error_share`` of their stopped time. Everything else stays non-stopping or uncategorised.
    """
    from wind_downtime.sources import load_dataset

    from wind_om.scada import SCADA, code_stop_share

    ds = load_dataset("hill_of_towie", [year], scada_dir)
    sc = SCADA.build(ds.fleet)
    y0, y1 = pd.Timestamp(f"{year}-01-01", tz="UTC"), pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
    sub = ev[(ev["start"] >= y0) & (ev["start"] < y1)]
    cat = code_stop_share(sub, sc, timers={"turbine_error": ds.extra["error_s"]}, min_intervals=6)
    desc = pd.read_csv(Path(data_dir) / "raw" / "hill_of_towie" / "Hill_of_Towie_alarms_description.csv")
    desc["code"] = desc["Alarm Code"].astype(str)
    pub = desc.set_index("code")
    rows = []
    allcodes = sorted(set(cat["code"]) | set(pub.index), key=lambda c: int(c))
    cs = cat.set_index("code")
    for c in allcodes:
        r = cs.loc[c] if c in cs.index else None
        if c in pub.index:
            rows.append((c, HOT_PUBLISHED_COMPONENT.get(c, ""), "", HOT_PUBLISHED_CATEGORY.get(c, ""),
                         str(int(pub.loc[c, "Stopping"])), pub.loc[c, "Description"].strip(" :"), "published"))
            continue
        if r is None or r["intervals"] < min_intervals:
            continue
        stopping = r["stop_share"] >= stop_share
        err = float(np.nan_to_num(r.get("turbine_error_share", 0.0)))
        category = "forced_outage" if stopping and err >= error_share else ""
        source = (f"inferred {year}: stopped in {r['stop_share']:.0%} of {int(r['intervals'])} intervals, "
                  f"turbine-error timer on {err:.0%} of that time")
        rows.append((c, "", "", category, "1" if stopping else "0", "", source))
    mapping = pd.DataFrame(rows, columns=["code", "component", "role", "category", "stopping", "description",
                                          "source"])
    return cat, mapping


# ---------------------------------------------------------------------------------------- SCADA years

def scada_checks(farm: str, a: Analysis, scada_dir: Path) -> tuple[pd.DataFrame, dict]:
    """Lost energy on the segments of the SCADA years; agreement, clock check and reference losses."""
    from wind_downtime.sources import load_dataset

    from wind_om.scada import SCADA, agreement, attribute_lost, clock_check, coverage

    seg = a.seg.copy()
    seg["lost_kwh"], seg["expected_kwh"] = np.nan, np.nan
    out = {"agreement": [], "monthly": [], "clock": None, "reference": []}
    for y in SCADA_YEARS[farm]:
        ds = load_dataset(farm, [y], scada_dir)
        sc = SCADA.build(ds.fleet)
        y0, y1 = pd.Timestamp(f"{y}-01-01", tz="UTC"), pd.Timestamp(f"{y + 1}-01-01", tz="UTC")
        m = (seg["start"] < y1) & (seg["end"] > y0)
        part = attribute_lost(seg[m], sc)
        seg.loc[m, ["lost_kwh", "expected_kwh"]] = part[["lost_kwh", "expected_kwh"]].to_numpy()
        stops = seg[m]
        ov, mon = agreement(stops, sc)
        out["agreement"].append({"year": y, **ov})
        out["monthly"].append(mon)
        cov = coverage(stops, sc)
        ref = {"year": y, "lost_mwh_ours": float(np.nansum(part["lost_kwh"])) / 1000}
        # logged stop time where SCADA has no usable row: its energy cannot be priced from the signals
        ref["stop_h_without_scada"] = float((cov * ~sc.valid).sum()) / 6
        if "gb_lost_downtime" in ds.extra:
            gb = np.nan_to_num(ds.extra["gb_lost_downtime"])
            ref["lost_mwh_greenbyte_downtime"] = float(gb.sum()) / 1000
            ref["lost_mwh_greenbyte_in_logged_stops"] = float(gb[cov > 0].sum()) / 1000
            ref["lost_mwh_greenbyte_without_scada"] = float(gb[(cov > 0) & ~sc.valid].sum()) / 1000
        if "shutdown_s" in ds.extra:
            sd = np.nan_to_num(ds.extra["shutdown_s"])
            ref["shutdown_h_operator"] = float(sd.sum()) / 3600
            ref["stop_h_log"] = float(cov.sum()) / 6
            ref["shutdown_h_operator_in_logged_stops"] = float(sd[cov >= 0.5].sum()) / 3600
        out["reference"].append(ref)
        if y == CLOCK_YEAR[farm]:
            out["clock"] = clock_check(stops, sc)
        log.info("%s %d: SCADA F1 %.2f, lost %.0f MWh", farm, y, ov["f1"], ref["lost_mwh_ours"])
    return seg, out


# ------------------------------------------------------------------------------------------ the study

def run_study(data_dir: Path, scada_dir: Path | None, results: Path, docs: Path,
              farms=("kelmarsh", "penmanshiel", "hill_of_towie")) -> dict:
    from wind_om.report import kpi_pack, write_excel, write_html
    from wind_om.sources import load_farm, log_window

    summaries, dash = {}, {}
    for farm in farms:
        ev, meta = load_farm(farm, data_dir)
        t0, t1 = log_window(ev, meta)
        out = Path(results) / farm
        out.mkdir(parents=True, exist_ok=True)
        if farm == "hill_of_towie":
            if scada_dir is None:
                raise ValueError("Hill of Towie needs --scada-dir for the code catalogue")
            cat, mp = hot_catalogue(ev, scada_dir, data_dir)
            cat.to_csv(out / "code_catalogue.csv", index=False)
            target = Path(__file__).parent / "mappings" / "siemens_swt_hot.csv"
            mp.to_csv(target, index=False, lineterminator="\n")
            log.info("Hill of Towie mapping: %d codes (%d inferred stopping) -> %s", len(mp),
                     int(((mp["source"] != "published") & (mp["stopping"] == "1")).sum()), target.name)
        mapping = read_mapping(meta["mapping"])
        # the open logs hold routine messages every day: a week of silence means missing files
        a = analyse(ev, mapping, t0, t1, settings=Settings(max_silence="7D"), origin=meta["cod"])
        checks = None
        if scada_dir is not None:
            a.seg, checks = scada_checks(farm, a, scada_dir)
        tables, summary = full_tables(a)
        summary.update({"farm": farm, "name": NAMES[farm], "events": len(a.ev),
                        "stops": int(a.ev["stop"].sum())})
        if checks:
            agr = pd.DataFrame(checks["agreement"])
            ref = pd.DataFrame(checks["reference"])
            agr.to_csv(out / "scada_agreement.csv", index=False)
            ref.to_csv(out / "scada_reference.csv", index=False)
            pd.concat(checks["monthly"]).to_csv(out / "scada_monthly_hours.csv", index=False)
            checks["clock"].to_csv(out / "clock_check.csv", index=False)
            best = checks["clock"].loc[checks["clock"]["f1"].idxmax()]
            summary.update({"scada_f1": float(np.average(agr["f1"], weights=agr["log_hours"])),
                            "scada_precision": float(np.average(agr["precision"], weights=agr["log_hours"])),
                            "scada_recall": float(np.average(agr["recall"], weights=agr["log_hours"])),
                            "clock_best_shift_h": float(best["shift_h"]), "clock_best_f1": float(best["f1"]),
                            "lost_mwh_scada_years": float(ref["lost_mwh_ours"].sum()),
                            "scada_years": SCADA_YEARS[farm]})
            if "lost_mwh_greenbyte_downtime" in ref:
                summary["lost_mwh_greenbyte_downtime"] = float(ref["lost_mwh_greenbyte_downtime"].sum())
            if "shutdown_h_operator" in ref:
                summary["shutdown_h_operator"] = float(ref["shutdown_h_operator"].sum())
                summary["stop_h_log_scada_years"] = float(ref["stop_h_log"].sum())
            mon = pd.concat(checks["monthly"])
            mon = mon[mon["valid_h"] > 24 * 20]
            summary["monthly_hours_corr"] = float(np.corrcoef(mon["log_h"], mon["scada_h"])[0, 1])
        for name, df in tables.items():
            df.to_csv(out / f"{name}.csv", index=False)
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=_json), encoding="utf-8")
        summaries[farm] = summary
        month = KPI_MONTH[farm]
        pack = kpi_pack(a, month, tz="UTC", farm=NAMES[farm])
        ex = Path(docs) / "examples"
        write_excel(pack, ex / f"kpi_{farm}_{month}.xlsx")
        write_html(pack, ex / f"kpi_{farm}_{month}.html")
        dash[farm] = dashboard_data(farm, a, tables, summary, checks)
        log.info("%s done: %d failures, MTBF %.0f h, MTTR %.1f h, trend %s", farm, summary["failures"],
                 summary["mtbf_h"], summary["mttr_h"], summary["trend"])
    pd.DataFrame(summaries).T.to_csv(Path(results) / "summary.csv")
    Path(docs).mkdir(parents=True, exist_ok=True)
    (Path(docs) / "data.js").write_text("window.OM_DATA = " + json.dumps(dash, default=_json, separators=(",", ":"))
                                        + ";\n", encoding="utf-8")
    return summaries


def _json(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return str(v)


def _records(df: pd.DataFrame, cols, digits: int = 2) -> list:
    d = df[cols].copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].round(digits)
        elif isinstance(d[c].dtype, pd.DatetimeTZDtype):
            d[c] = d[c].dt.strftime("%Y-%m-%dT%H:%M")
    return [[None if (isinstance(v, float) and not np.isfinite(v)) else v for v in row]
            for row in d.itertuples(index=False)]


def dashboard_data(farm: str, a: Analysis, t: dict, s: dict, checks) -> dict:
    """Compact data for docs/index.html: Pareto by year, failure timeline, trend, bad actors, checks."""
    years = _years(a)
    par = {}
    for key, n in (("code", 25), ("component", 20)):
        par[key] = {}
        for y in ["all"] + years:
            if y == "all":
                p = pareto(a.ev, a.seg, a.ep, key)
            else:
                p = pareto(a.ev, a.seg, a.ep, key, pd.Timestamp(f"{y}-01-01", tz="UTC"),
                           pd.Timestamp(f"{y + 1}-01-01", tz="UTC"))
            if "lost_mwh" not in p:
                p["lost_mwh"] = np.nan
            if key == "code":
                p["label"] = p["code"] + " " + p["description"].str.slice(0, 32)
            else:
                p["label"] = p["component"]
            par[key][str(y)] = _records(p.head(n), ["label", "hours", "stops", "failures", "lost_mwh"], 1)
    f = a.res.merge(a.rep[["episode_id", "is_repeat"]], on="episode_id", how="left")
    timeline = _records(f, ["turbine_id", "start", "duration_h", "code", "description", "component", "restoration",
                            "is_repeat"], 2)
    duane = t["duane"]
    step = max(len(duane) // 400, 1)
    ca = t["crow_amsaa"].iloc[0]
    fit = R.duane_fit(duane, float(a.expo["hours"].sum()))
    out = {
        "name": NAMES[farm], "summary": s, "years": years, "turbines": a.turbines,
        "pareto": par, "timeline": timeline,
        "yearly": _records(t["yearly"], [c for c in t["yearly"].columns], 3),
        "yearly_cols": list(t["yearly"].columns),
        "duane": _records(duane.iloc[::step], ["fleet_hours", "cum_failures"], 0),
        "crow": {"beta": ca["crow_beta"], "lam": ca["crow_lambda"], "fleet": fit,
                 "total_years": float(a.expo["hours"].sum()) / R.HOURS_PER_YEAR},
        "jack": _records(t["jackknife_codes"].head(60), ["code", "failures", "mttr_h", "downtime_h", "quadrant"], 2),
        "jack_limits": [float(t["jackknife_codes"]["n_limit"].iloc[0]),
                        float(t["jackknife_codes"]["mttr_limit_h"].iloc[0])] if len(t["jackknife_codes"]) else None,
        "jack_desc": dict(zip(t["pareto_codes"]["code"], t["pareto_codes"]["description"])),
        "mtbf": _records(t["mtbf_turbines"].merge(t["bad_actor_turbines"][["turbine_id", "ratio", "q",
                                                                            "bad_actor"]], on="turbine_id"),
                         ["turbine_id", "failures", "mtbf_h", "mtbf_lo_h", "mtbf_hi_h", "mttr_h", "ttr_median_h",
                          "downtime_h", "ratio", "q", "bad_actor"], 4),
        "components": _records(t["mtbf_components"], ["component", "failures", "failures_per_turbine_year",
                                                      "mtbf_h", "mttr_h", "ttr_median_h", "downtime_h"], 3),
        "repeats": _records(t["repeats"].head(12), ["code", "description", "failures", "repeats", "repeat_rate",
                                                    "chance_rate"], 3) if len(t["repeats"]) else [],
        "cascades": _records(t["cascade_pairs"].head(12), ["primary", "primary_text", "secondary", "secondary_text",
                                                           "count", "confidence", "lift"], 2)
        if len(t["cascade_pairs"]) else [],
        "restoration": _records(t["restoration"], list(t["restoration"].columns), 2),
        "spares": _records(t["spares"].head(10), ["component", "failures", "expected_per_year_fleet",
                                                  "lead_time_demand_mean", "stock", "p_stockout"], 3),
        "weibull": _records(t["weibull"], list(t["weibull"].columns), 3),
    }
    if checks:
        out["agreement"] = _records(pd.DataFrame(checks["agreement"]), ["year", "precision", "recall", "f1",
                                                                        "log_hours", "scada_hours"], 3)
        out["clock"] = _records(checks["clock"], ["shift_h", "f1"], 3)
        mon = pd.concat(checks["monthly"])
        mon = mon[mon["valid_h"] > 24 * 20]
        out["monthly"] = _records(mon, ["month", "turbine_id", "log_h", "scada_h"], 1)
        out["reference"] = _records(pd.DataFrame(checks["reference"]),
                                    list(pd.DataFrame(checks["reference"]).columns), 1)
        out["reference_cols"] = list(pd.DataFrame(checks["reference"]).columns)
    return out


# ------------------------------------------------------------------------------------- demo and export

def demo(out: Path) -> None:
    """A synthetic farm in Polish local time: the log, its YAML, its mapping and a KPI pack."""
    import yaml

    from wind_om.report import kpi_pack, write_excel, write_html
    from wind_om.schema import read_log, write_log
    from wind_om.synthetic import mapping_frame, synthetic_log

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lg, _ = synthetic_log(n_turbines=12, years=2, process="hpp", mtbf_h=450, start="2025-01-01",
                          repeat_prob=0.1, cascade_prob=0.5, onsite_prob=0.5, grid_losses=3, gaps=2, seed=42)
    raw = lg[["turbine_id", "start", "end", "code", "description", "category"]].copy()
    raw["category"] = raw["category"].map({"forced_outage": "Fault", "grid": "Grid", "communication": "Comms",
                                           "scheduled_maintenance": "Service"})
    raw = raw.rename(columns={"turbine_id": "WTG", "start": "Start", "end": "End", "code": "Code",
                              "description": "Text", "category": "Type"})
    write_log(raw.rename(columns={"Start": "start", "End": "end"}), out / "demo_log.csv", tz="Europe/Warsaw")
    cfg = {"format": "csv", "columns": {"turbine_id": "WTG", "start": "start", "end": "end", "code": "Code",
                                        "description": "Text", "category": "Type"},
           "timezone": "Europe/Warsaw", "ambiguous": "infer", "datetime_format": "%Y-%m-%d %H:%M:%S",
           "stop": {"column": "Type", "values": ["Fault", "Grid", "Service"]},
           "category_map": {"Fault": "forced_outage", "Grid": "grid", "Comms": "communication",
                            "Service": "scheduled_maintenance"}}
    (out / "demo_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    mapping_frame().to_csv(out / "demo_mapping.csv", index=False)
    ev = read_log(out / "demo_log.csv", out / "demo_config.yaml")
    a = analyse(ev, read_mapping(out / "demo_mapping.csv"))
    pack = kpi_pack(a, "2026-10", tz="Europe/Warsaw", farm="Synthetic farm (12 turbines)")
    write_excel(pack, out / "kpi_2026-10.xlsx")
    write_html(pack, out / "kpi_2026-10.html")
    log.info("demo written to %s", out)


def dst_roundtrip(farm: str, data_dir: Path, tz: str, results: Path) -> dict:
    """Write an open farm's log as naive local times in ``tz``, read it back, compare with the UTC original."""
    import io

    from wind_om.schema import COLUMNS, normalise
    from wind_om.sources import load_farm

    ev, _ = load_farm(farm, data_dir)
    ev = ev[list(COLUMNS)]
    local = ev.copy()
    for c in ("start", "end"):
        local[c] = local[c].dt.tz_convert(tz).dt.strftime("%d.%m.%Y %H:%M:%S")
    buf = io.StringIO()
    local.to_csv(buf, index=False, sep=";")
    cfg = {"read": {"sep": ";"}, "timezone": tz, "ambiguous": "infer", "datetime_format": "%d.%m.%Y %H:%M:%S",
           "stop": {"column": "stop", "values": ["True"]}}
    back = normalise(pd.read_csv(io.StringIO(buf.getvalue()), sep=";", dtype=str, keep_default_na=False,
                                 na_values=[""]), cfg)
    a = ev.sort_values(["turbine_id", "start", "code"], kind="stable").reset_index(drop=True)
    b = back.sort_values(["turbine_id", "start", "code"], kind="stable").reset_index(drop=True)
    wall = ev["start"].dt.tz_convert(tz).dt.tz_localize(None)
    amb = (wall.dt.tz_localize(tz, ambiguous=np.ones(len(wall), dtype=bool)) !=
           wall.dt.tz_localize(tz, ambiguous=np.zeros(len(wall), dtype=bool)))
    ds = (b["start"] - a["start"]).abs()
    de = (b["end"] - a["end"]).abs().fillna(pd.Timedelta(0))
    out = {"farm": farm, "tz": tz, "events": len(a), "ambiguous_starts": int(amb.sum()),
           "start_mismatches": int((ds > pd.Timedelta(0)).sum()), "end_mismatches": int((de > pd.Timedelta(0)).sum()),
           "max_error_h": float(max(ds.max(), de.max()) / pd.Timedelta("1h"))}
    p = Path(results) / farm / "dst_roundtrip.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("DST round trip %s", out)
    return out


def export_farm(farm: str, data_dir: Path, out: Path, tz: str = "UTC") -> None:
    """An open farm's canonical log as a CSV (naive times in ``tz``) with the YAML to read it back."""
    import shutil
    from importlib import resources

    import yaml

    from wind_om.schema import COLUMNS, write_log
    from wind_om.sources import MAPPINGS, load_farm

    ev, _ = load_farm(farm, data_dir)
    out = Path(out)
    write_log(ev[list(COLUMNS)], out / f"{farm}_log.csv", tz=None if tz == "UTC" else tz)
    cfg = {"format": "csv", "timezone": tz, "ambiguous": "infer", "datetime_format": "%Y-%m-%d %H:%M:%S",
           "stop": {"column": "stop", "values": ["True"]}}
    (out / f"{farm}_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    src = resources.files("wind_om") / "mappings" / f"{MAPPINGS[farm]}.csv"
    shutil.copy(str(src), out / f"{MAPPINGS[farm]}.csv")
    log.info("%s: %d events written to %s", farm, len(ev), out)
