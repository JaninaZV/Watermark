"""v0.3 enterprise trust: grades, operator disclosures, audit pack, gate, compare-regions."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from audit_pack import build_assumption_lineage, create_audit_pack, methodology_hash
from compare_regions import COMPARISON_SCHEMA_VERSION, build_comparison
from gate import evaluate_gate
from measurement_grade import compute_measurement_grade
from operator_disclosures import get_operator_disclosure, regions_with_disclosures
from schema_contract import RUN_SCHEMA_VERSION, validate_run_summary
import watermark_meter as wm


def _base_summary(**overrides) -> dict:
    summary = {
        "schema_version": RUN_SCHEMA_VERSION,
        "measurement_grade": "C",
        "grade_limiting_factor": "default_pue",
        "per_unit": None,
        "run_metadata": {
            "started_at_utc": "2026-05-30T22:36:13+00:00",
            "ended_at_utc": "2026-05-30T22:38:33+00:00",
            "duration_s": 140.0,
            "samples": 140,
            "host_os": "Linux",
            "rapl_platform": None,
            "scope": "host",
            "workload_type": "unknown",
            "methodology_hash": methodology_hash(),
        },
        "measured_sources": {
            "cpu": ["modeled_from_util"],
            "gpu": ["nvml_pynvml"],
            "cpu_rapl_samples": 0,
            "cpu_modeled_samples": 140,
            "gpu_missing_samples": 0,
        },
        "energy": {
            "it_cpu_wh": 1.0,
            "it_cpu_source": ["modeled_from_util"],
            "it_gpu_wh": 10.0,
            "it_gpu_source": ["nvml_pynvml"],
            "it_total_kwh": 0.011,
            "it_total_source": "sum_it_components",
            "facility_total_kwh": 0.01738,
            "facility_source": "pue_multiplier",
        },
        "carbon": {"co2e_kg": 0.006, "source": "static_avg", "energy_basis_kwh": 0.01738},
        "water": {
            "direct_cooling_l": 0.001,
            "indirect_generation_l": 0.03,
            "total_l": 0.031,
            "direct_source": "modeled_wue",
            "indirect_source": "static_avg",
            "stress_score": 0.25,
            "stress_level": "low-medium",
            "stress_basin": "Lower Chesapeake",
            "stress_source": "wri_aqueduct_2023",
            "stress_weighted_total_l": 0.03875,
            "wwl_l": 0.03875,
            "wwl_ml": 38.75,
            "water_accounting_method": "consumption",
            "water_source": "static_avg",
            "weighting_methodology": "multiplier_1_plus_score, see methodology",
            "gpu_water_fraction": 0.91,
            "gpu_water_attribution_method": "energy_proportional",
        },
        "embodied": {
            "co2e_kg": 0.0,
            "water_l": 0.0,
            "embodied_water_ml": None,
            "sku": None,
            "useful_life_hours": None,
            "source": "no_profile",
            "citation": None,
            "label": None,
        },
        "lifecycle": {
            "carbon": {"operational_kg": 0.006, "embodied_kg": 0.0, "total_kg": 0.006},
            "water": {"operational_l": 0.031, "embodied_l": 0.0, "total_l": 0.031},
        },
        "assumptions": {
            "region": "us-east-1",
            "region_label": "US Virginia",
            "grid_source": "static",
            "pue": 1.58,
            "pue_source": "default_iea_2024",
            "wue_source": "region_default",
            "cooling_type": "unknown",
            "water_accounting_method": "consumption",
            "water_stress_season": "annual",
            "water_stress_as_of": "2026-05-30T22:36:13Z",
        },
        "caveats": [],
    }
    summary.update(overrides)
    return summary


class TestMeasurementGrade:
    def test_default_run_is_grade_c(self):
        grade, factor = compute_measurement_grade(_base_summary(), rapl_available_at_start=False)
        assert grade == "C"
        assert factor == "cooling_type_unknown"

    def test_rapl_interrupted_is_grade_c(self):
        summary = _base_summary()
        summary["measured_sources"]["cpu_rapl_samples"] = 50
        summary["measured_sources"]["cpu_modeled_samples"] = 10
        summary["assumptions"]["cooling_type"] = "evaporative"
        summary["assumptions"]["pue_source"] = "user_override"
        summary["assumptions"]["wue_source"] = "user_override"
        grade, factor = compute_measurement_grade(summary, rapl_available_at_start=True)
        assert grade == "C"
        assert factor == "rapl_interrupted"


class TestOperatorDisclosures:
    def test_registry_has_regions(self):
        assert len(regions_with_disclosures()) >= 10

    def test_us_east_1_withdrawal(self):
        d = get_operator_disclosure("us-east-1")
        assert d is not None
        assert d["accounting_method"] == "withdrawal"
        assert d["disclosure_verified"] is False

    def test_operator_water_profile_source_tag(self):
        profile = wm.fetch_water_profile("us-east-1", water_source="operator")
        assert profile["water_source"] == wm.WATER_SOURCE_OPERATOR
        assert profile["accounting_method"] == "withdrawal"


class TestCompareRegions:
    def test_inversion_between_virginia_and_sweden(self):
        payload = build_comparison(
            ["us-east-1", "eu-north-1"],
            workload="embeddings",
            it_kwh=None,
            reference_run=None,
            pue=wm.DEFAULT_PUE,
            cooling_type="unknown",
            water_source="static",
            grid_source="static",
        )
        assert payload["schema_version"] == COMPARISON_SCHEMA_VERSION
        assert "water_carbon_inversion" in payload
        assert payload["winners"]["lowest_carbon_region"] == "eu-north-1"


class TestGate:
    def test_fails_when_wwl_exceeded(self):
        summary = _base_summary(per_unit={
            "unit_type": "token",
            "unit_count": 1000,
            "wwl_ml_per_unit": 600.0,
            "energy_wh_per_unit": 1.0,
            "carbon_g_per_unit": 0.01,
        })
        ok, msg = evaluate_gate(summary, max_wwl_ml_per_unit=500, max_carbon_g_per_unit=None)
        assert not ok
        assert "FAIL" in msg

    def test_passes_with_grade_c_warning(self):
        summary = _base_summary(per_unit={
            "unit_type": "token",
            "unit_count": 1000,
            "wwl_ml_per_unit": 100.0,
            "energy_wh_per_unit": 1.0,
            "carbon_g_per_unit": 0.01,
        })
        ok, msg = evaluate_gate(summary, max_wwl_ml_per_unit=500, max_carbon_g_per_unit=None)
        assert ok
        assert "grade C" in msg or "PASS" in msg


class TestAuditPack:
    def test_audit_pack_reproducible(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        summary = _base_summary()
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (run_dir / "measurements.csv").write_text("timestamp,cpu_watts\n2026-01-01T00:00:00,10\n")
        out1 = tmp_path / "a.zip"
        out2 = tmp_path / "b.zip"
        create_audit_pack(run_dir, out1)
        create_audit_pack(run_dir, out2)
        assert out1.read_bytes() == out2.read_bytes()
        with zipfile.ZipFile(out1) as zf:
            names = set(zf.namelist())
        assert {"summary.json", "measurements.csv", "METHODOLOGY.md", "caveats.json", "README.md"} <= names

    def test_assumption_lineage_includes_grade(self):
        lineage = build_assumption_lineage(_base_summary())
        fields = {e["field"] for e in lineage["assumptions"]}
        assert "measurement_grade" in fields
        assert "wwl_ml" in fields


class TestSchemaV03:
    def test_canonical_v03_validates(self):
        assert validate_run_summary(_base_summary()) == []

    def test_per_unit_block_validates(self):
        summary = _base_summary(per_unit={
            "unit_type": "token",
            "unit_count": 1_000_000,
            "wwl_ml_per_unit": 0.047,
            "energy_wh_per_unit": 0.0012,
            "carbon_g_per_unit": 0.003,
        })
        assert validate_run_summary(summary) == []


class TestPerUnitAndWorkload:
    def test_build_per_unit_token(self):
        grid = wm.static_grid_profile("us-east-1")
        water = wm.fetch_water_profile("us-east-1")
        impacts = wm.compute_impacts(0.01, 0.0158, grid, water)
        block = wm.build_per_unit_block(impacts, unit_type="token", unit_count=1000, facility_wh=15.8)
        assert block["unit_type"] == "token"
        assert block["wwl_ml_per_unit"] > 0

    def test_infer_training(self):
        assert wm.infer_workload_type(training_steps=1000) == "training"
        assert wm.infer_workload_type(token_count=500) == "inference"
