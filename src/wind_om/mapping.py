"""Code-to-component mapping, one CSV per manufacturer or controller family.

=============  ===============================================================================================
column         meaning
=============  ===============================================================================================
code           the status / alarm code as it appears in the log (required)
component      subsystem the code points to (``COMPONENTS`` is the vocabulary used here; others are allowed)
role           ``onsite`` (someone at the turbine: manual stop on site, service mode, manual operation),
               ``remote`` (remote stop / reset), empty otherwise - used to tell resets from interventions
category       reason category (``schema.CATEGORIES``); fills events whose log category is unknown
stopping       1 / 0: the code stops the turbine; fills events whose log has no stop flag
description    free text (documentation only)
=============  ===============================================================================================

The files shipped in ``wind_om/mappings`` were written from the public code texts of the open datasets; they
are a starting point, not an OEM document. Codes missing from a mapping get the component ``unmapped``.

Usage:
    m = read_mapping(Path("mappings/senvion_mm.csv"))
    log = apply_mapping(log, m)       # adds component and role, fills category and stop
"""
from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import pandas as pd

from wind_om.schema import CATEGORIES

log = logging.getLogger(__name__)

COMPONENTS = {
    "pitch": "Pitch system (drives, batteries, converters, encoders)",
    "rotor_blades": "Blades and hub",
    "brake_hydraulics": "Mechanical brake and hydraulic system",
    "gearbox": "Gearbox and its lubrication / cooling",
    "drivetrain": "Main shaft, bearings, couplings, drivetrain oscillations",
    "generator": "Generator, slip rings, brushes",
    "converter": "Frequency converter",
    "electrical": "Transformer, switchgear, cabinets, UPS, auxiliary supply",
    "yaw": "Yaw system and cable twist",
    "sensors": "Wind and other sensors (anemometer, vane, vibration, temperature)",
    "control": "Controller, safety chain, communication",
    "tower_nacelle": "Tower, nacelle, cooling and heating, lights",
    "grid": "Grid (external): voltage, frequency, grid loss",
    "environment": "Wind, icing, temperature, shadow / bat / noise curtailment",
    "operator": "Operator / grid operator requests, park controller",
    "service": "Manual stops, service and maintenance, tests",
    "unmapped": "Code not in the mapping",
}
ROLES = ("", "onsite", "remote")


def read_mapping(path) -> pd.DataFrame:
    """Read a mapping CSV (or the name of a shipped one, e.g. ``senvion_mm``)."""
    p = Path(path)
    if not p.exists():
        shipped = resources.files("wind_om") / "mappings" / f"{p.stem}.csv"
        if not shipped.is_file():
            raise FileNotFoundError(f"mapping {path} not found (shipped: {shipped_mappings()})")
        p = Path(str(shipped))
    m = pd.read_csv(p, dtype=str, keep_default_na=False, comment="#")
    if "code" not in m.columns:
        raise ValueError(f"{p.name}: a 'code' column is required")
    m["code"] = m["code"].str.strip().str.replace(r"\.0$", "", regex=True)
    for c in ("component", "role", "category", "stopping", "description"):
        if c not in m.columns:
            m[c] = ""
    m["role"] = m["role"].str.strip().str.lower()
    bad_role = sorted(set(m["role"]) - set(ROLES))
    if bad_role:
        raise ValueError(f"{p.name}: unknown roles {bad_role}; allowed: onsite, remote or empty")
    bad_cat = sorted(set(m["category"]) - set(CATEGORIES) - {""})
    if bad_cat:
        raise ValueError(f"{p.name}: unknown categories {bad_cat}; allowed: {list(CATEGORIES)}")
    other = sorted(set(m["component"]) - set(COMPONENTS) - {""})
    if other:
        log.info("%s: components outside the default vocabulary: %s", p.name, other[:10])
    dup = m["code"].duplicated()
    if dup.any():
        log.warning("%s: %d duplicated codes, first kept", p.name, int(dup.sum()))
        m = m[~dup]
    return m.reset_index(drop=True)


def shipped_mappings() -> list[str]:
    return sorted(f.name[:-4] for f in (resources.files("wind_om") / "mappings").iterdir()
                  if f.name.endswith(".csv"))


def apply_mapping(df: pd.DataFrame, mapping: pd.DataFrame | None, default_stop: bool = False) -> pd.DataFrame:
    """Add ``component`` and ``role``; fill unknown categories and missing stop flags from the mapping."""
    out = df.copy()
    if mapping is None or mapping.empty:
        out["component"], out["role"] = "unmapped", ""
        out["stop"] = out["stop"].fillna(default_stop).astype(bool)
        return out
    m = mapping.set_index("code")
    comp = out["code"].map(m["component"]).replace("", pd.NA)
    out["component"] = comp.fillna("unmapped").astype(str)
    out["role"] = out["code"].map(m["role"]).fillna("").astype(str)
    cat = out["code"].map(m["category"]).replace("", pd.NA)
    fill = out["category"].eq("unknown") & cat.notna()
    out.loc[fill, "category"] = cat[fill]
    stopping = out["code"].map(m["stopping"]).replace("", pd.NA).map({"1": True, "0": False})
    out["stop"] = out["stop"].fillna(stopping.astype("boolean")).fillna(default_stop).astype(bool)
    n_unmapped = int(out["component"].eq("unmapped").sum())
    if n_unmapped:
        top = out.loc[out["component"].eq("unmapped"), "code"].value_counts().head(8).to_dict()
        log.info("%d events with codes outside the mapping, most frequent: %s", n_unmapped, top)
    return out


def mapping_template(df: pd.DataFrame) -> pd.DataFrame:
    """An empty mapping for every code of a log, most frequent first, to fill in by hand."""
    g = (df.groupby("code").agg(description=("description", lambda s: s.mode().iloc[0] if len(s) else ""),
                                events=("code", "size"))
         .sort_values("events", ascending=False).reset_index())
    for c in ("component", "role", "category", "stopping"):
        g[c] = ""
    return g[["code", "component", "role", "category", "stopping", "description", "events"]]
