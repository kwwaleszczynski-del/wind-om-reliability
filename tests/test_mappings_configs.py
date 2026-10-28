"""The shipped mappings and YAML configs read and behave as documented."""
from importlib import resources
from pathlib import Path

import pandas as pd
import pytest

from wind_om.mapping import COMPONENTS, apply_mapping, mapping_template, read_mapping, shipped_mappings
from wind_om.schema import CATEGORIES, load_config, normalise


def test_shipped_mappings_are_valid():
    assert "senvion_mm" in shipped_mappings()
    m = read_mapping("senvion_mm")
    assert m["code"].is_unique and set(m["component"]) <= set(COMPONENTS)
    assert set(m["role"]) <= {"", "onsite", "remote"}
    assert m.set_index("code").loc["20", "role"] == "onsite"
    assert m.set_index("code").loc["3110", "component"] == "converter"


@pytest.mark.parametrize("name", ["greenbyte", "hill_of_towie", "example_vestas", "example_siemens_gamesa",
                                  "example_nordex", "example_senvion"])
def test_configs_parse(name):
    cfg = load_config(Path(str(resources.files("wind_om") / "configs" / f"{name}.yaml")))
    assert {"turbine_id", "start", "code"} <= set(cfg["columns"])
    assert set(cfg.get("category_map", {}).values()) <= set(CATEGORIES)


def test_greenbyte_layout_and_mapping():
    raw = pd.DataFrame({
        "turbine_id": ["T1", "T1", "T1", "T1"],
        "Timestamp start": ["2020-01-01 10:00:00", "2020-01-01 10:00:05", "2020-01-01 12:00:00", "2020-01-02"],
        "Timestamp end": ["2020-01-01 11:00:00", "2020-01-01 10:30:00", None, "2020-01-02 01:00:00"],
        "Code": ["3110", "1050", "6410", "9997"], "Message": ["Frequency converter error", "Drivetrain osc.",
                                                              "Manual yaw", "Data communication unavailable"],
        "IEC category": ["Forced outage", None, "Full Performance", "Communication"],
        "Status": ["Stop", "Warning", "Informational", "Communication"]})
    cfg = load_config(Path(str(resources.files("wind_om") / "configs" / "greenbyte.yaml")))
    ev = apply_mapping(normalise(raw, cfg), read_mapping("senvion_mm"))
    assert ev["stop"].tolist() == [True, False, False, False]
    assert ev["category"].tolist() == ["forced_outage", "unknown", "information", "communication"]
    assert ev["component"].tolist() == ["converter", "drivetrain", "service", "control"]
    assert ev["role"].tolist() == ["", "", "onsite", ""]
    tpl = mapping_template(ev)
    assert list(tpl.columns[:5]) == ["code", "component", "role", "category", "stopping"]


def test_mapping_fills_unknown_category_and_stop_flag(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("code,component,role,category,stopping\n7,pitch,,forced_outage,1\n8,sensors,,,0\n", encoding="utf-8")
    raw = pd.DataFrame({"turbine_id": ["A", "A"], "start": ["2024-05-01 10:00", "2024-05-01 11:00"],
                        "end": ["2024-05-01 10:30", "2024-05-01 11:10"], "code": ["7", "8"]})
    ev = apply_mapping(normalise(raw), read_mapping(p))
    assert ev["category"].tolist() == ["forced_outage", "unknown"]
    assert ev["stop"].tolist() == [True, False]
    bad = tmp_path / "bad.csv"
    bad.write_text("code,role\n1,boss\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_mapping(bad)
