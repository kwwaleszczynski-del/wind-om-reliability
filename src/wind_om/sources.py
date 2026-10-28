"""Adapters from the three open datasets to the generic log, through the same YAML mappings a user would write.

=============  ===========================  ===============================================================
farm           turbines                     event log
=============  ===========================  ===============================================================
Kelmarsh       6 x Senvion MM92, 2.05 MW    Greenbyte status log 2016-2021: Stop / Warning / Informational /
                                            Communication, code, message, IEC category of every event
Penmanshiel    14 x Senvion MM82, 2.05 MW   the same, 2016-2024
Hill of Towie  21 x Siemens SWT-2.3-VS-82   controller alarm log 2016-2026: code, time on, time off; 12 of
                                            about 400 codes are described (with a stopping flag)
=============  ===========================  ===============================================================

Kelmarsh is read from the Parquet cache or the archives of the ``wind_power_curve`` package
(``KELMARSH_DATA_DIR``); Penmanshiel and Hill of Towie from the logs downloaded by ``fetch``.

Usage:
    log, meta = load_farm("penmanshiel", Path("data"))
    meta["cod"]       # {turbine_id: commissioning date}, the age origin for Crow-AMSAA
"""
from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from pathlib import Path

import pandas as pd

from wind_om.schema import load_config, normalise

log = logging.getLogger(__name__)

FARMS = ("kelmarsh", "penmanshiel", "hill_of_towie")
MAPPINGS = {"kelmarsh": "senvion_mm", "penmanshiel": "senvion_mm", "hill_of_towie": "siemens_swt_hot"}
CONFIGS = {"kelmarsh": "greenbyte", "penmanshiel": "greenbyte", "hill_of_towie": "hill_of_towie"}
RATED_KW = {"kelmarsh": 2050.0, "penmanshiel": 2050.0, "hill_of_towie": 2300.0}
HOT_STATION_OFFSET = 2304509  # StationNr = turbine number + offset


def shipped_config(name: str) -> dict:
    from importlib import resources

    return load_config(Path(str(resources.files("wind_om") / "configs" / f"{name}.yaml")))


def kelmarsh_dir() -> Path:
    return Path(os.environ.get("KELMARSH_DATA_DIR", "data/kelmarsh"))


def _greenbyte_static(path: Path, width: int) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig").dropna(subset=["Title"])
    num = df["Title"].str.extract(r"(\d+)$", expand=False).astype(int)
    return pd.DataFrame({"turbine_id": [f"T{n:0{width}d}" for n in num],
                         "cod": pd.to_datetime(df["Commercial Operations Date"], dayfirst=True).dt.tz_localize("UTC"),
                         "rated_kw": df["Rated power (kW)"].astype(float)})


def _greenbyte_raw(status: pd.DataFrame) -> pd.DataFrame:
    """Greenbyte status rows -> the export layout ``configs/greenbyte.yaml`` describes."""
    raw = status.rename(columns={"start": "Timestamp start", "end": "Timestamp end", "status": "Status",
                                 "code": "Code", "message": "Message", "iec_category": "IEC category"})
    raw["Code"] = raw["Code"].astype("Int64").astype(str)
    # data-communication gaps carry no IEC category: give them the communication category
    raw["IEC category"] = raw["IEC category"].where(raw["Status"].ne("Communication"), "Communication")
    return raw


def read_kelmarsh(data_dir: Path | None = None) -> pd.DataFrame:
    from wind_power_curve.scada import read_status_csv

    kdir = data_dir or kelmarsh_dir()
    parts = []
    for y in range(2016, 2022):
        cache = kdir / "processed" / f"status_{y}.parquet"
        if cache.exists():
            parts.append(pd.read_parquet(cache))
            continue
        for arc in sorted((kdir / "raw").glob(f"Kelmarsh_SCADA_{y}_*.zip")):
            with zipfile.ZipFile(arc) as z:
                for name in z.namelist():
                    m = re.match(r"Status_Kelmarsh_(\d+)_", name)
                    if m:
                        with z.open(name) as fh:
                            parts.append(read_status_csv(io.TextIOWrapper(fh, encoding="utf-8-sig"), int(m[1])))
    if not parts:
        raise FileNotFoundError(f"no Kelmarsh status logs under {kdir}; set KELMARSH_DATA_DIR")
    st = pd.concat(parts, ignore_index=True).drop_duplicates()
    st["turbine_id"] = "T" + st.pop("turbine").astype(int).astype(str)
    return _greenbyte_raw(st)


def read_penmanshiel(data_dir: Path) -> pd.DataFrame:
    from wind_power_curve.scada import read_status_csv

    raw = Path(data_dir) / "raw" / "penmanshiel"
    parts = []
    for arc in sorted(raw.glob("status_*.zip")):
        with zipfile.ZipFile(arc) as z:
            for name in z.namelist():
                m = re.match(r"Status_Penmanshiel_(\d+)_", name)
                if m:
                    with z.open(name) as fh:
                        df = read_status_csv(io.TextIOWrapper(fh, encoding="utf-8-sig"), 0)
                    parts.append(df.drop(columns="turbine").assign(turbine_id=f"T{int(m[1]):02d}"))
    if not parts:
        raise FileNotFoundError(f"no Penmanshiel status logs in {raw}; run 'python -m wind_om fetch'")
    # the quarterly 2023 exports overlap the yearly ones at their edges
    st = pd.concat(parts, ignore_index=True).drop_duplicates(["turbine_id", "start", "end", "code"])
    return _greenbyte_raw(st)


def read_hill_of_towie(data_dir: Path) -> pd.DataFrame:
    raw = Path(data_dir) / "raw" / "hill_of_towie"
    parts = []
    for arc in sorted(raw.glob("alarms_*.zip")):
        with zipfile.ZipFile(arc) as z:
            for name in sorted(z.namelist()):
                parts.append(pd.read_csv(z.open(name), dtype=str))
    if not parts:
        raise FileNotFoundError(f"no Hill of Towie alarm logs in {raw}; run 'python -m wind_om fetch'")
    al = pd.concat(parts, ignore_index=True)
    al["Turbine"] = "T" + (pd.to_numeric(al["StationNr"]) - HOT_STATION_OFFSET).astype(int).map("{:02d}".format)
    return al


def load_farm(farm: str, data_dir: Path, kelmarsh_data: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Canonical log of an open farm plus its metadata (cod, rated_kw, turbines, mapping name)."""
    data_dir = Path(data_dir)
    cfg = shipped_config(CONFIGS[farm])
    if farm == "kelmarsh":
        kdir = kelmarsh_data or kelmarsh_dir()
        raw = read_kelmarsh(kdir)
        static = _greenbyte_static(kdir / "raw" / "Kelmarsh_WT_static.csv", 1)
    elif farm == "penmanshiel":
        raw = read_penmanshiel(data_dir)
        static = _greenbyte_static(data_dir / "raw" / "penmanshiel" / "Penmanshiel_WT_static.csv", 2)
    elif farm == "hill_of_towie":
        raw = read_hill_of_towie(data_dir)
        meta = pd.read_csv(data_dir / "raw" / "hill_of_towie" / "Hill_of_Towie_turbine_metadata.csv",
                           encoding="utf-8-sig")
        static = pd.DataFrame({"turbine_id": meta["Turbine Name"],
                               "cod": pd.to_datetime(meta["Commercial Operations Date"]).dt.tz_localize("UTC"),
                               "rated_kw": meta["Rated power (kW)"].astype(float)})
    else:
        raise ValueError(f"unknown farm {farm}; choose from {FARMS}")
    ev = normalise(raw, cfg, source=farm)
    other = ~ev["turbine_id"].isin(static["turbine_id"])
    if other.any():  # e.g. a station number that is not one of the farm's turbines
        log.info("%s: %d events of %s dropped (not in the turbine list)", farm, int(other.sum()),
                 sorted(ev.loc[other, "turbine_id"].unique()))
        ev = ev[~other].reset_index(drop=True)
    if farm == "kelmarsh":  # commissioning tests before COD are not operation
        ev = ev[ev["end"].isna() | (ev["end"] > static["cod"].min())].reset_index(drop=True)
    meta = {"cod": dict(zip(static["turbine_id"], static["cod"])), "rated_kw": RATED_KW[farm],
            "turbines": sorted(static["turbine_id"]), "mapping": MAPPINGS[farm], "farm": farm}
    log.info("%s: %d events, %d turbines, %s - %s", farm, len(ev), ev["turbine_id"].nunique(),
             ev["start"].min(), ev["start"].max())
    return ev, meta


def log_window(ev: pd.DataFrame, meta: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Observation window: from the later of COD and the first record to the start of the last record's month."""
    t0 = max(min(meta["cod"].values()), ev["start"].min()).ceil("D")
    t1 = ev["start"].max().tz_convert("UTC").tz_localize(None).to_period("M").to_timestamp().tz_localize("UTC")
    return t0, t1
