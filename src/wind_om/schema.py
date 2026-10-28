"""The generic event-log format, its YAML column mapping and explicit time-zone handling.

Every analysis in this package runs on one table, one row per logged event:

============  ========================================================================  ==========
column        meaning                                                                   required
============  ========================================================================  ==========
turbine_id    any string                                                                yes
start         event start (UTC after reading)                                           yes
end           event end; empty for instantaneous or still-open events                   no
code          status / alarm code, any string                                           yes
description   the controller's text                                                     no
category      the log's own reason category, mapped onto ``CATEGORIES``                 no
stop          True when the event stops the turbine                                     no
============  ========================================================================  ==========

A YAML file says which column of an export is which, the time zone of naive timestamps and how raw category
and status values translate (examples for several manufacturers in ``wind_om/configs``)::

    format: csv                     # csv | excel | parquet
    read: {sep: ";", encoding: utf-8-sig}
    columns: {turbine_id: WTG, start: Start, end: End, code: Code, description: Text, category: Type}
    duration: {column: Duration, unit: s}     # instead of an end column
    timezone: Europe/Warsaw         # time zone of naive timestamps; UTC if they are UTC
    ambiguous: infer                # the repeated hour when DST ends: infer | earliest | latest | raise
    datetime_format: "%d.%m.%Y %H:%M:%S"
    stop: {column: Type, values: [Fault, Stop]}   # or: stop: all
    category_map: {Fault: forced_outage, Service: scheduled_maintenance}
    drop: {Type: [Info]}            # rows to ignore

Daylight saving time. Local wall-clock times are ambiguous for one hour each autumn (02:00-03:00 occurs twice
in Europe/Warsaw) and non-existent for one hour each spring. With ``ambiguous: infer`` a repeated time is
resolved from the order in which the log lists each turbine's events: the first pass is summer time, and the
clock running backwards marks the second pass. An end time is resolved to the earliest reading not before its
start. Non-existent spring times are shifted forward; both cases are counted in the log messages.

Usage:
    log = read_log(Path("alarms.csv"), Path("configs/my_oem.yaml"))
    log.dtypes          # turbine_id str, start/end datetime64[ns, UTC], code str, category str, stop bool
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

log = logging.getLogger(__name__)

COLUMNS = ("turbine_id", "start", "end", "code", "description", "category", "stop")

# Reason categories, after the information categories of IEC 61400-26-1 (time-based availability). The
# standard's tree is finer; mapping a contract onto these should be checked against the contract itself.
CATEGORIES = {
    "forced_outage": "Forced outage (fault)",
    "scheduled_maintenance": "Scheduled maintenance",
    "planned_corrective": "Planned corrective action",
    "environmental": "Out of environmental specification (low/high wind, icing, temperature)",
    "grid": "Out of electrical specification (grid)",
    "requested": "Requested shutdown or curtailment (grid operator or owner)",
    "technical_standby": "Technical standby (tests, cable unwind, start-up)",
    "communication": "Communication lost / information unavailable",
    "information": "Information or warning without a reason category",
    "unknown": "Not categorised",
}
FAILURE_CATEGORIES = ("forced_outage",)


def load_config(config) -> dict:
    """A YAML path, a YAML string or a dict -> dict (``None`` -> the canonical format as is)."""
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    path = Path(config)
    if path.suffix.lower() in (".yaml", ".yml") and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return yaml.safe_load(str(config)) or {}


def _read_raw(path: Path, cfg: dict) -> pd.DataFrame:
    fmt = cfg.get("format") or {".parquet": "parquet", ".pq": "parquet", ".xlsx": "excel",
                                ".xls": "excel"}.get(path.suffix.lower(), "csv")
    opts = dict(cfg.get("read") or {})
    if fmt == "parquet":
        return pd.read_parquet(path)
    if fmt == "excel":
        return pd.read_excel(path, dtype=object, **opts)
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], **opts)


def _parse_wall_clock(values: pd.Series, cfg: dict) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        return values
    fmt, dayfirst = cfg.get("datetime_format"), bool(cfg.get("dayfirst", False))
    if fmt:
        return pd.to_datetime(values, format=fmt, errors="coerce")
    s = values.astype("string").str.strip()
    has_offset = s.str.contains(r"(?:[+-]\d{2}:?\d{2}|Z)$", regex=True, na=False)
    if has_offset.any():
        return pd.to_datetime(s, utc=True, errors="coerce", dayfirst=dayfirst, format="mixed")
    return pd.to_datetime(s, errors="coerce", dayfirst=dayfirst, format="mixed")


def _first_pass(local: pd.Series, ambiguous: np.ndarray, groups: pd.Series | None) -> np.ndarray:
    """True for ambiguous wall times logged before the clock went back (summer time), in file order."""
    out = np.ones(len(local), dtype=bool)
    if not ambiguous.any():
        return out
    pos = np.flatnonzero(ambiguous)
    keys = pd.DataFrame({"g": (groups.to_numpy()[pos] if groups is not None else 0),
                         "d": local.to_numpy()[pos].astype("datetime64[D]"), "pos": pos})
    unresolved = 0
    for _, grp in keys.groupby(["g", "d"], sort=False):
        p = grp["pos"].to_numpy()  # file order
        t = local.to_numpy()[p]
        back = np.flatnonzero(t[1:] < t[:-1])
        if len(back):
            out[p[back[0] + 1:]] = False
        else:
            unresolved += len(p)
    if unresolved:
        log.warning("%d ambiguous local times without a visible clock change: taken as summer time", unresolved)
    return out


def to_utc(values: pd.Series, tz: str = "UTC", ambiguous: str = "infer", groups: pd.Series | None = None,
           not_before: pd.Series | None = None, cfg: dict | None = None) -> pd.Series:
    """Timestamps (strings or datetimes, naive or with an offset) -> UTC.

    Naive values are wall-clock times in ``tz``. ``not_before`` (UTC) resolves each ambiguous value to its
    earliest reading at or after it (used for end times); otherwise ``ambiguous`` decides.
    """
    ts = _parse_wall_clock(pd.Series(values).reset_index(drop=True), cfg or {})
    if ts.dt.tz is not None:
        return ts.dt.tz_convert("UTC")
    if str(tz).upper() == "UTC":
        return ts.dt.tz_localize("UTC")
    n = len(ts)
    summer = ts.dt.tz_localize(tz, ambiguous=np.ones(n, dtype=bool), nonexistent="shift_forward")
    winter = ts.dt.tz_localize(tz, ambiguous=np.zeros(n, dtype=bool), nonexistent="shift_forward")
    amb = (summer != winter).to_numpy() & ts.notna().to_numpy()
    gap = ts.notna() & (ts.dt.tz_localize(tz, ambiguous=np.ones(n, dtype=bool), nonexistent="NaT").isna())
    if gap.any():
        log.warning("%d local times fall in the spring DST gap (%s): shifted forward", int(gap.sum()), tz)
    if not_before is not None:
        nb = pd.Series(not_before).reset_index(drop=True)
        first = (summer >= nb).to_numpy() | nb.isna().to_numpy()
    elif ambiguous == "infer":
        first = _first_pass(ts, amb, None if groups is None else pd.Series(groups).reset_index(drop=True))
    elif ambiguous in ("earliest", "latest"):
        first = np.full(n, ambiguous == "earliest")
    elif ambiguous == "raise":
        if amb.any():
            raise ValueError(f"{int(amb.sum())} ambiguous local times (DST end) in {tz}")
        first = np.ones(n, dtype=bool)
    else:
        raise ValueError(f"ambiguous must be infer | earliest | latest | raise, not {ambiguous!r}")
    if amb.any():
        log.info("%d ambiguous local times (DST end) resolved", int(amb.sum()))
    out = summer.where(first | ~amb, winter)
    return out.dt.tz_convert("UTC")


def _clean_code(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    return s.fillna("")


def read_log(path, config=None) -> pd.DataFrame:
    """Read an event export with its YAML mapping and return the canonical table (``COLUMNS``)."""
    path = Path(path)
    cfg = load_config(config)
    raw = _read_raw(path, cfg)
    return normalise(raw, cfg, source=path.name)


def normalise(raw: pd.DataFrame, config=None, source: str = "log") -> pd.DataFrame:
    """Raw table + mapping -> canonical table. Also accepts an already canonical table (no config)."""
    cfg = load_config(config)
    raw = raw.reset_index(drop=True)
    for col, values in (cfg.get("drop") or {}).items():
        raw = raw[~raw[col].astype(str).isin([str(v) for v in values])].reset_index(drop=True)
    cols = {**{c: c for c in COLUMNS}, **(cfg.get("columns") or {})}
    missing = [k for k in ("turbine_id", "start", "code") if cols[k] not in raw.columns]
    if missing:
        raise ValueError(f"{source}: no column for {missing}; columns found: {list(raw.columns)}")
    tz, amb = cfg.get("timezone", "UTC"), cfg.get("ambiguous", "infer")
    out = pd.DataFrame({"turbine_id": raw[cols["turbine_id"]].astype("string").str.strip()})
    out["start"] = to_utc(raw[cols["start"]], tz, amb, groups=out["turbine_id"], cfg=cfg)
    dur = cfg.get("duration")
    if cols["end"] in raw.columns:
        out["end"] = to_utc(raw[cols["end"]], tz, amb, not_before=out["start"], cfg=cfg)
    elif dur:
        val = pd.to_numeric(raw[dur["column"]], errors="coerce")
        out["end"] = out["start"] + pd.to_timedelta(val, unit=dur.get("unit", "s"))
    else:
        out["end"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
    out["code"] = _clean_code(raw[cols["code"]])
    desc = cols["description"]
    out["description"] = raw[desc].astype("string").fillna("") if desc in raw.columns else ""
    cat_col = cols["category"]
    if cat_col in raw.columns:
        cmap = {str(k): v for k, v in (cfg.get("category_map") or {}).items()}
        rawcat = raw[cat_col].astype("string").fillna("")
        cat = rawcat.map(lambda v: cmap.get(v, v if v in CATEGORIES else "unknown"))
        unmapped = sorted(set(rawcat[~rawcat.isin(list(cmap)) & ~rawcat.isin(list(CATEGORIES)) & rawcat.ne("")]))
        if unmapped:
            log.warning("%s: category values without a mapping (-> unknown): %s", source, unmapped[:12])
        out["category"] = cat.astype(str)
    else:
        out["category"] = "unknown"
    bad = sorted(set(out["category"]) - set(CATEGORIES))
    if bad:
        raise ValueError(f"category_map points to unknown categories {bad}; allowed: {list(CATEGORIES)}")
    stop_cfg = cfg.get("stop")
    if stop_cfg == "all":
        out["stop"] = pd.array([True] * len(out), dtype="boolean")
    elif isinstance(stop_cfg, dict):
        vals = [str(v) for v in stop_cfg.get("values", [])]
        out["stop"] = pd.array(raw[stop_cfg["column"]].astype(str).isin(vals).to_numpy(), dtype="boolean")
    elif "stop" in raw.columns and cols["stop"] == "stop":
        out["stop"] = raw["stop"].astype("boolean")
    else:
        out["stop"] = pd.array([pd.NA] * len(out), dtype="boolean")  # decided by the code mapping
    return validate(out, source)


def validate(df: pd.DataFrame, source: str = "log") -> pd.DataFrame:
    """Check and tidy a canonical table: types, unparsed times, ends before starts, exact duplicates."""
    out = df.copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = {"end": pd.NaT, "description": "", "category": "unknown", "stop": pd.NA}[c]
    out["start"] = pd.to_datetime(out["start"], utc=True)
    out["end"] = pd.to_datetime(out["end"], utc=True)
    out["turbine_id"] = out["turbine_id"].astype(str)
    out["code"] = _clean_code(out["code"]).astype(str)
    out["description"] = out["description"].astype("string").fillna("").astype(str)
    out["stop"] = out["stop"].astype("boolean")
    bad_t = out["start"].isna()
    if bad_t.any():
        log.warning("%s: %d rows without a readable start dropped", source, int(bad_t.sum()))
        out = out[~bad_t]
    neg = out["end"] < out["start"]
    if neg.any():
        log.warning("%s: %d events end before they start: end removed", source, int(neg.sum()))
        out.loc[neg, "end"] = pd.NaT
    n0 = len(out)
    out = out.drop_duplicates(["turbine_id", "start", "end", "code"])
    if len(out) < n0:
        log.info("%s: %d duplicated rows dropped", source, n0 - len(out))
    # file order breaks ties between events logged in the same second (the first listed is the first-up)
    if "seq" not in out.columns:
        out["seq"] = np.arange(len(out))
    keep = list(COLUMNS) + ["seq"] + [c for c in ("component", "role") if c in out.columns]
    return out[keep].sort_values(["turbine_id", "start", "seq"], kind="stable").reset_index(drop=True)


def write_log(df: pd.DataFrame, path: Path, tz: str | None = None) -> Path:
    """Write a canonical table (CSV or Parquet); ``tz`` writes naive local wall-clock times instead of UTC."""
    out = df.copy()
    if tz:
        for c in ("start", "end"):
            out[c] = out[c].dt.tz_convert(tz).dt.tz_localize(None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".parquet":
        out.to_parquet(path, index=False)
    else:
        out.to_csv(path, index=False, date_format="%Y-%m-%d %H:%M:%S")
    return path
