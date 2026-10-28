"""Monthly O&M KPI pack: one Excel workbook and one self-contained HTML page per farm and month.

Contents (month = the reported calendar month in local time; 12 months = the twelve months up to its end):

==================  =============================================================================  ==========
sheet               content                                                                        window
==================  =============================================================================  ==========
Summary             headline KPIs, stop hours and lost energy by category, Crow-AMSAA trend         both
Pareto codes        downtime, share, cumulative share, stops, first-up, failures, lost MWh          month
Pareto components   the same by component                                                          month
MTBF turbines       failures, operating hours, MTBF with 90 % interval, MTTR, median, P90           12 months
MTBF components     the same by component                                                          12 months
Bad actors          turbines above the fleet rate (Poisson + FDR), code x turbine pairs,           12 months
                    jack-knife quadrants of the codes
Repeats             repeat failures of the month; repeat rate per code against chance             both
Cascades            most frequent primary -> secondary alarm pairs                                  12 months
Restoration         auto reset / remote / on site / not logged, response time                      both
Trend               failures, MTBF, stop hours by category and lost MWh per month                  history
Failures            every failure of the month                                                     month
==================  =============================================================================  ==========

Usage:
    pack = kpi_pack(analysis, "2026-09", tz="Europe/Warsaw", farm="Farm A")
    write_excel(pack, Path("out/kpi_2026-09.xlsx")); write_html(pack, Path("out/kpi_2026-09.html"))
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from wind_om import compound, events
from wind_om import reliability as R
from wind_om.pareto import monthly_hours, pareto, vital_few
from wind_om.schema import CATEGORIES

log = logging.getLogger(__name__)


@dataclass
class Pack:
    farm: str
    month: str
    tz: str
    summary: dict
    tables: dict = field(default_factory=dict)  # sheet name -> DataFrame
    notes: dict = field(default_factory=dict)   # sheet name -> one-line explanation


def _clip(df: pd.DataFrame, a, b) -> pd.DataFrame:
    out = df[(df["end"] > a) & (df["start"] < b)].copy()
    out["start"], out["end"] = out["start"].clip(lower=a), out["end"].clip(upper=b)
    out["hours"] = (out["end"] - out["start"]).dt.total_seconds() / 3600
    return out


def kpi_pack(a, month: str, tz: str = "Europe/Warsaw", farm: str = "farm", history_months: int = 12) -> Pack:
    """All KPI tables of one month (see module docstring). ``a`` is an ``analysis.Analysis``."""
    per = events.periods(a.start, a.end, tz=tz).set_index("period")
    if month not in per.index:
        raise ValueError(f"{month} is outside the log window {per.index[0]} - {per.index[-1]}")
    m0, m1 = per.loc[month, "start"], per.loc[month, "end"]
    pos = per.index.get_loc(month)
    w0 = per["start"].iloc[max(pos - history_months + 1, 0)]
    up_w = _clip(a.up, w0, m1)
    f_m = a.fail[(a.fail["start"] >= m0) & (a.fail["start"] < m1)]
    f_w = a.fail[(a.fail["start"] >= w0) & (a.fail["start"] < m1)]
    has_lost = "lost_kwh" in a.seg

    pc = pareto(a.ev, a.seg, a.ep, "code", m0, m1)
    pk = pareto(a.ev, a.seg, a.ep, "component", m0, m1)
    by_cat = pareto(a.ev, a.seg, a.ep, "category", m0, m1).set_index("category")
    turb = R.mtbf_mttr(f_w, up_w, by=["turbine_id"])
    comp = R.mtbf_mttr(f_w, up_w, by=["component"])
    bad_t = R.turbine_outliers(f_w, up_w)
    bad_ct = R.code_turbine_outliers(f_w, up_w)
    jk = R.jackknife(f_w, key="code")
    rep_m = a.rep[(a.rep["start"] >= m0) & (a.rep["start"] < m1)]
    rep_w = a.rep[(a.rep["start"] >= w0) & (a.rep["start"] < m1)]
    rep_sum = compound.repeat_summary(rep_w, operating_h=up_w["hours"].sum(), days=a.settings.repeat_days)
    res_m = a.res[(a.res["start"] >= m0) & (a.res["start"] < m1)]
    res_w = a.res[(a.res["start"] >= w0) & (a.res["start"] < m1)]
    rs_m, rs_w = compound.restoration_summary(res_m), compound.restoration_summary(res_w)
    cas_w = a.cas[(a.cas["start"] >= w0) & (a.cas["start"] < m1)]
    pairs = compound.cascade_pairs(cas_w, a.ev.groupby("code")["description"].first())
    ca = R.crow_amsaa(a.fail[a.fail["start"] < m1], _clip(a.expo, a.start, m1), a.origin)

    hist = per.iloc[max(pos - 23, 0):pos + 1].reset_index()
    trend = monthly_hours(a.seg, a.ev, hist, "category")
    trend.columns = [f"stop_h_{c}" for c in trend.columns]
    trend.insert(0, "failures", [int(((a.fail["start"] >= s) & (a.fail["start"] < e)).sum())
                                 for s, e in zip(hist["start"], hist["end"])])
    op = [float(_clip(a.up, s, e)["hours"].sum()) for s, e in zip(hist["start"], hist["end"])]
    trend.insert(1, "operating_h", op)
    trend.insert(2, "mtbf_h", trend["operating_h"] / trend["failures"].replace(0, np.nan))
    if has_lost:
        trend["lost_mwh"] = [pareto(a.ev, a.seg, a.ep, "category", s, e)["lost_mwh"].sum()
                             for s, e in zip(hist["start"], hist["end"])]
    trend = trend.reset_index()

    n_t = a.expo["turbine_id"].nunique()
    hours_m = float(per.loc[month, "hours"])
    fail_h = float(f_m["duration_h"].sum())
    summary = {
        "Farm": farm, "Month": month, "Time zone": tz, "Turbines": n_t, "Hours in month": hours_m,
        "Failures (month)": len(f_m), "Failures (12 months)": len(f_w),
        "Failures per turbine-year (12 months)": len(f_w) / (up_w["hours"].sum() / R.HOURS_PER_YEAR)
        if up_w["hours"].sum() else np.nan,
        "MTBF h (12 months)": up_w["hours"].sum() / len(f_w) if len(f_w) else np.nan,
        "MTTR h (month)": f_m["duration_h"].mean() if len(f_m) else np.nan,
        "MTTR h (12 months)": f_w["duration_h"].mean() if len(f_w) else np.nan,
        "Failure downtime h (month)": fail_h,
        "Repeat failures (month)": int(rep_m["is_repeat"].sum()),
        "Repeat rate (12 months)": rep_w["is_repeat"].mean() if len(rep_w) else np.nan,
        "Auto resets share (month)": float((res_m["restoration"] == "auto_reset").mean()) if len(res_m) else np.nan,
        "Median on-site response h (12 months)": rs_w.set_index("restoration")["response_median_h"].get(
            "on_site", np.nan) if len(rs_w) else np.nan,
        "Codes making 80 % of downtime (month)": vital_few(pc),
        "Bad-actor turbines (12 months)": ", ".join(bad_t.loc[bad_t["bad_actor"], "turbine_id"]) or "none",
        "Crow-AMSAA beta (to month end)": ca.beta, "Reliability trend": ca.verdict,
    }
    for c in CATEGORIES:
        if c in by_cat.index and by_cat.loc[c, "hours"] > 0:
            summary[f"Stop h: {c}"] = float(by_cat.loc[c, "hours"])
    if has_lost:
        summary["Lost MWh (month, all stops)"] = float(by_cat["lost_mwh"].sum())
        for c in CATEGORIES:
            if c in by_cat.index and by_cat.loc[c, "lost_mwh"] > 0:
                summary[f"Lost MWh: {c}"] = float(by_cat.loc[c, "lost_mwh"])

    fcols = ["turbine_id", "start", "end", "duration_h", "code", "description", "component", "first_code",
             "n_events", "restoration", "response_h"]
    fm = res_m[[c for c in fcols if c in res_m]].copy()
    for c in ("start", "end"):
        fm[c] = fm[c].dt.tz_convert(tz).dt.tz_localize(None)
    rm = rep_m[rep_m["is_repeat"]][["turbine_id", "start", "code", "description", "days_since_previous",
                                     "chain_pos", "duration_h"]].copy()
    rm["start"] = rm["start"].dt.tz_convert(tz).dt.tz_localize(None)
    bad = pd.concat([
        bad_t.assign(table="turbine vs fleet rate"),
        bad_ct[bad_ct["bad_actor"]].assign(table="code on one turbine"),
    ], ignore_index=True)
    tables = {
        "Pareto codes": pc, "Pareto components": pk, "MTBF turbines": turb, "MTBF components": comp,
        "Bad actors": bad, "Jack-knife codes": jk, "Repeats": rm, "Repeat rate": rep_sum,
        "Cascades": pairs.head(40), "Restoration": pd.concat([rs_m.assign(window="month"),
                                                              rs_w.assign(window="12 months")]),
        "Trend": trend, "Failures": fm,
    }
    notes = {
        "Pareto codes": "Attributed stop hours (first-up rule, no double counting), all categories; rows inside "
                        "80 % cumulative share are the vital few.",
        "Pareto components": "The same grouped by component (code mapping).",
        "MTBF turbines": "Failure episodes (forced outage, not fleet-wide). MTBF = operating hours / failures, "
                         "90 % chi-square interval. MTTR = mean time to restore (first stop to restart).",
        "MTBF components": "Operating hours of the whole fleet per component.",
        "Bad actors": "One-sided exact Poisson test against the fleet rate, Benjamini-Hochberg q < 0.05.",
        "Jack-knife codes": "Knights' jack-knife: failures vs mean restore time; limits = mean failures per code "
                            "and the overall MTTR.",
        "Repeats": f"The same code on the same turbine within {a.settings.repeat_days:g} days after restart.",
        "Repeat rate": "chance_rate = repeat rate expected from the code's own frequency alone (Poisson).",
        "Cascades": f"Alarms starting within {a.settings.cascade_window} after the first-up stop of an episode.",
        "Restoration": f"on_site / remote from the code roles; auto_reset = nothing logged and restored within "
                       f"{a.settings.auto_max}; response = failure start to first on-site event.",
        "Trend": "Per local-time month.",
        "Failures": "Times in local time.",
    }
    return Pack(farm, month, tz, summary, tables, notes)


# ---------------------------------------------------------------------------------------------- Excel

HEADER_FILL, HEADER_FONT = "1F3B57", "FFFFFF"


def write_excel(pack: Pack, path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"O&M KPI pack - {pack.farm} - {pack.month}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Working material generated from the event log; definitions in the sheet notes."])
    ws.append([])
    ws.append(["KPI", "Value"])
    for c in ws[4]:
        c.fill, c.font = PatternFill("solid", fgColor=HEADER_FILL), Font(bold=True, color=HEADER_FONT)
    for k, v in pack.summary.items():
        ws.append([k, None if isinstance(v, float) and not np.isfinite(v) else v])
        cell = ws.cell(ws.max_row, 2)
        if isinstance(v, float):
            cell.number_format = "0.0%" if ("share" in k.lower() or "rate (" in k.lower()) else "#,##0.0"
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 44, 22
    red = PatternFill("solid", fgColor="F8CBAD")
    for row in ws.iter_rows(min_row=5):
        if row[0].value == "Reliability trend" and row[1].value == "deteriorating":
            row[1].fill = red
        if row[0].value == "Bad-actor turbines (12 months)" and row[1].value not in ("none", None):
            row[1].fill = red

    for name, df in pack.tables.items():
        ws = wb.create_sheet(name[:31])
        ws.append([pack.notes.get(name, "")])
        ws["A1"].font = Font(italic=True, color="555555")
        out = df.copy()
        for c in out.columns:
            if isinstance(out[c].dtype, pd.DatetimeTZDtype):
                out[c] = out[c].dt.tz_convert(pack.tz).dt.tz_localize(None)
            elif out[c].dtype == object:
                out[c] = out[c].map(lambda v: ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else v)
        ws.append(list(out.columns))
        for c in ws[2]:
            c.fill, c.font = PatternFill("solid", fgColor=HEADER_FILL), Font(bold=True, color=HEADER_FONT)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for row in out.itertuples(index=False):
            ws.append([None if (isinstance(v, float) and not np.isfinite(v)) or v is pd.NaT else
                       (v.item() if hasattr(v, "item") else v) for v in row])
        ws.freeze_panes = "A3"
        n = ws.max_row
        cols = {c: i + 1 for i, c in enumerate(out.columns)}
        for c, i in cols.items():
            letter = get_column_letter(i)
            width = max([len(str(c))] + [len(str(v)) for v in out[c].head(200).tolist()]) + 2
            ws.column_dimensions[letter].width = min(max(width, 9), 46)
            fmt = None
            if c in ("share", "cum_share", "repeat_rate", "chance_rate", "excess", "confidence", "ratio_share"):
                fmt = "0.0%"
            elif out[c].dtype.kind == "f":
                fmt = "#,##0.00" if c in ("p", "q", "lift", "ratio", "crow_beta") else "#,##0.0"
            elif out[c].dtype.kind == "M":
                fmt = "yyyy-mm-dd hh:mm"
            if fmt:
                for r in range(3, n + 1):
                    ws.cell(r, i).number_format = fmt
        if n < 3:
            continue

        def rng(col, cols=cols, n=n):
            letter = get_column_letter(cols[col])
            return f"{letter}3:{letter}{n}"

        for c in ("hours", "downtime_h", "failure_downtime_h", "lost_mwh", "count"):
            if c in cols:
                ws.conditional_formatting.add(rng(c), DataBarRule(start_type="min", end_type="max",
                                                                  color="5B9BD5"))
        if "cum_share" in cols:
            letter = get_column_letter(cols["cum_share"])
            ws.conditional_formatting.add(
                f"A3:{get_column_letter(len(cols))}{n}",
                FormulaRule(formula=[f"${letter}3<=0.8"], fill=PatternFill("solid", fgColor="FFF2CC")))
        if "mtbf_h" in cols:
            ws.conditional_formatting.add(rng("mtbf_h"), ColorScaleRule(
                start_type="percentile", start_value=10, start_color="F8696B", mid_type="percentile",
                mid_value=50, mid_color="FFEB84", end_type="percentile", end_value=90, end_color="63BE7B"))
        for c in ("mttr_h", "ttr_p90_h", "response_h", "response_median_h"):
            if c in cols:
                ws.conditional_formatting.add(rng(c), ColorScaleRule(
                    start_type="min", start_color="63BE7B", end_type="max", end_color="F8696B"))
        if "bad_actor" in cols:
            letter = get_column_letter(cols["bad_actor"])
            ws.conditional_formatting.add(f"A3:{get_column_letter(len(cols))}{n}", FormulaRule(
                formula=[f"${letter}3=TRUE"], fill=PatternFill("solid", fgColor="F8CBAD")))
        if "quadrant" in cols:
            ws.conditional_formatting.add(rng("quadrant"), CellIsRule(
                operator="equal", formula=['"acute+chronic"'], fill=PatternFill("solid", fgColor="F8CBAD")))
        if "excess" in cols:
            ws.conditional_formatting.add(rng("excess"), CellIsRule(
                operator="greaterThan", formula=["0.1"], fill=PatternFill("solid", fgColor="F8CBAD")))
    wb.save(path)
    log.info("wrote %s", path)
    return path


# ----------------------------------------------------------------------------------------------- HTML

CSS = """
:root { --page:#f9f9f7; --surface:#fff; --ink:#111; --ink2:#555; --grid:#e3e2dc; --bar:#2a78d6; --hot:#f6d3c3;
        --vital:#fff3cd; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --page:#121212; --surface:#1c1c1b; --ink:#eee; --ink2:#bbb;
        --grid:#333; --bar:#3987e5; --hot:#5a2d1f; --vital:#3d3517; color-scheme: dark; } }
body { margin:0; background:var(--page); color:var(--ink); font:14px/1.45 system-ui,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:20px 16px 48px; }
h1 { font-size:1.45rem; margin:0 0 4px; } h2 { font-size:1.1rem; margin:28px 0 4px; }
p.note { color:var(--ink2); margin:2px 0 8px; font-size:.86rem; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:14px 0; }
.tile { background:var(--surface); border:1px solid var(--grid); border-radius:8px; padding:10px 12px; }
.tile .v { font-size:1.35rem; font-weight:650; } .tile .l { color:var(--ink2); font-size:.8rem; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.82rem; background:var(--surface); }
th, td { padding:3px 7px; border-bottom:1px solid var(--grid); text-align:left; white-space:nowrap; }
td.n { text-align:right; font-variant-numeric:tabular-nums; }
tr.bad td { background:var(--hot); } tr.vital td { background:var(--vital); }
.bar { display:inline-block; height:9px; background:var(--bar); border-radius:2px; vertical-align:middle;
       margin-right:6px; }
svg text { fill:var(--ink2); font-size:11px; }
"""


def _fmt(v, col=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or v is pd.NaT:
        return ""
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, (list, tuple)):
        return html.escape(", ".join(map(str, v)))
    if isinstance(v, (bool, np.bool_)):
        return "yes" if v else ""
    if isinstance(v, (float, np.floating)):
        if col in ("repeat_rate", "chance_rate", "excess", "confidence") or "share" in col or "rate (" in col:
            return f"{v:.1%}"
        if col in ("p", "q"):
            return f"{v:.3g}"
        return f"{v:,.1f}" if abs(v) >= 10 else f"{v:,.2f}"
    return html.escape(str(v))


def _table(df: pd.DataFrame, max_rows: int = 30, bar: str | None = None) -> str:
    if df is None or df.empty:
        return "<p class='note'>Nothing in this window.</p>"
    d = df.head(max_rows)
    mx = float(d[bar].max()) if bar and bar in d and len(d) and d[bar].max() > 0 else None
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in d.columns)
    body = []
    for _, r in d.iterrows():
        cls = "bad" if bool(r.get("bad_actor", False)) or r.get("quadrant") == "acute+chronic" else (
            "vital" if "cum_share" in d and r["cum_share"] <= 0.8 + 1e-9 else "")
        cells = []
        for c in d.columns:
            v = r[c]
            num = isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_))
            txt = _fmt(v, c)
            if c == bar and mx:
                txt = f"<span class='bar' style='width:{max(float(v) / mx * 90, 1):.0f}px'></span>{txt}"
            cells.append(f"<td class='n'>{txt}</td>" if num else f"<td>{txt}</td>")
        body.append(f"<tr class='{cls}'>{''.join(cells)}</tr>")
    more = f"<p class='note'>{len(df) - max_rows} more rows in the Excel file.</p>" if len(df) > max_rows else ""
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>" \
           f"</div>{more}"


def pareto_svg(p: pd.DataFrame, label: str = "code", n: int = 15, width: int = 760, height: int = 260) -> str:
    """Bars of hours and the cumulative share line, as inline SVG."""
    d = p.head(n)
    if d.empty:
        return ""
    left, right, top, bottom = 46, 40, 12, 70
    w, h = width - left - right, height - top - bottom
    mx = float(d["hours"].max()) or 1.0
    bw = w / len(d)
    parts = [(f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Pareto of downtime' "
              f"style='width:100%;max-width:{width}px'>")]
    for k in range(5):
        y = top + h - h * k / 4
        parts.append(f"<line x1='{left}' x2='{left + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='var(--grid)'/>"
                     f"<text x='{left - 6}' y='{y + 4:.1f}' text-anchor='end'>{mx * k / 4:,.0f}</text>"
                     f"<text x='{left + w + 6}' y='{y + 4:.1f}'>{k * 25}%</text>")
    pts = []
    for i, (_, r) in enumerate(d.iterrows()):
        x = left + i * bw
        bh = h * r["hours"] / mx
        lab = html.escape(str(r[label]))[:18]
        parts.append(f"<rect x='{x + bw * 0.12:.1f}' y='{top + h - bh:.1f}' width='{bw * 0.76:.1f}' "
                     f"height='{bh:.1f}' fill='var(--bar)'><title>{lab}: {r['hours']:,.1f} h</title></rect>"
                     f"<text transform='translate({x + bw / 2:.1f},{top + h + 8}) rotate(45)'>{lab}</text>")
        pts.append(f"{x + bw / 2:.1f},{top + h - h * r['cum_share']:.1f}")
    parts.append(f"<polyline points='{' '.join(pts)}' fill='none' stroke='#eb6834' stroke-width='2'/>")
    parts.append("</svg>")
    return "".join(parts)


def write_html(pack: Pack, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    s = pack.summary
    tiles = [("Failures (month)", s["Failures (month)"]), ("MTBF h (12 months)", s["MTBF h (12 months)"]),
             ("MTTR h (month)", s["MTTR h (month)"]), ("Repeat failures (month)", s["Repeat failures (month)"]),
             ("Auto resets share (month)", s["Auto resets share (month)"]),
             ("Reliability trend", s["Reliability trend"])]
    if "Lost MWh (month, all stops)" in s:
        tiles.insert(1, ("Lost MWh (month, all stops)", s["Lost MWh (month, all stops)"]))
    tile_html = "".join(f"<div class='tile'><div class='v'>{_fmt(v, k.lower())}</div><div class='l'>"
                        f"{html.escape(k)}</div></div>" for k, v in tiles)
    t = pack.tables
    summ = pd.DataFrame({"KPI": list(s), "Value": [_fmt(v, k.lower()) for k, v in s.items()]})
    sections = [
        ("Summary", "", _table(summ, 60)),
        ("Pareto by code", pack.notes["Pareto codes"],
         pareto_svg(t["Pareto codes"]) + _table(t["Pareto codes"], 25, "hours")),
        ("Pareto by component", pack.notes["Pareto components"],
         pareto_svg(t["Pareto components"], "component") + _table(t["Pareto components"], 20, "hours")),
        ("MTBF and MTTR per turbine (12 months)", pack.notes["MTBF turbines"], _table(t["MTBF turbines"], 40)),
        ("MTBF and MTTR per component (12 months)", pack.notes["MTBF components"],
         _table(t["MTBF components"], 20)),
        ("Bad actors (12 months)", pack.notes["Bad actors"], _table(t["Bad actors"], 30)),
        ("Jack-knife of codes (12 months)", pack.notes["Jack-knife codes"], _table(t["Jack-knife codes"], 20)),
        ("Repeat failures (month)", pack.notes["Repeats"], _table(t["Repeats"], 30)),
        ("Repeat rate per code (12 months)", pack.notes["Repeat rate"], _table(t["Repeat rate"], 20)),
        ("Alarm cascades (12 months)", pack.notes["Cascades"], _table(t["Cascades"], 20)),
        ("Restoration and response", pack.notes["Restoration"], _table(t["Restoration"], 10)),
        ("Monthly trend", pack.notes["Trend"], _table(t["Trend"], 24)),
        ("Failures of the month", pack.notes["Failures"], _table(t["Failures"], 60)),
    ]
    body = "".join(f"<h2>{html.escape(h)}</h2><p class='note'>{html.escape(n)}</p>{c}" for h, n, c in sections)
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width, initial-scale=1'><title>O&amp;M KPI {html.escape(pack.month)}</title>"
           f"<style>{CSS}</style></head><body><main><h1>O&amp;M KPI pack: {html.escape(pack.farm)}, "
           f"{html.escape(pack.month)}</h1><p class='note'>Generated from the turbine event log. Times in "
           f"{html.escape(pack.tz)}. Working material; check definitions against the contract before use."
           f"</p><div class='tiles'>{tile_html}</div>{body}</main></body></html>")
    path.write_text(doc, encoding="utf-8")
    log.info("wrote %s", path)
    return path
