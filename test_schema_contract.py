"""Contract tests for summary.json and portfolio payload schemas (see SCHEMA.md)."""

from __future__ import annotations

import io
from contextlib import redirect_stderr

from dashboard.portfolio import build_portfolio_payload
from schema_contract import (
    RUN_SCHEMA_VERSION,
    make_caveat,
    normalize_caveat,
    resolve_run_schema_version,
    validate_portfolio_payload,
    validate_run_summary,
    warn_missing_run_schema_version,
)
import watermark_meter as wm

CANONICAL_CAVEATS = [
    make_caveat(
        "grid_carbon_static_avg",
        "info",
        "Grid carbon intensity is a regional annual average (static_avg). "
        "For real-time attribution use --grid-source electricitymaps.",
    ),
    make_caveat(
        "grid_intensity_annual_average",
        "warning",
        "Grid carbon/water intensities are annual regional averages.",
    ),
]

CANONICAL_SUMMARY = {
    "schema_version": RUN_SCHEMA_VERSION,
    "run_metadata": {
        "started_at_utc": "2026-05-30T22:36:13+00:00",
        "ended_at_utc": "2026-05-30T22:38:33+00:00",
        "duration_s": 140.0,
        "samples": 140,
        "host_os": "Linux",
        "rapl_platform": None,
        "scope": "host",
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
    "carbon": {
        "co2e_kg": 0.006084,
        "source": "static_avg",
        "energy_basis_kwh": 0.01738,
    },
    "water": {
        "direct_cooling_l": 0.00132,
        "indirect_generation_l": 0.033022,
        "total_l": 0.034342,
        "direct_source": "modeled_wue",
        "indirect_source": "static_avg",
        "stress_score": 0.35,
        "stress_level": "medium-high",
        "stress_basin": "Potomac River",
        "stress_source": "wri_aqueduct_2023",
        "stress_weighted_total_l": 0.046362,
        "wwl_l": 0.046362,
        "wwl_ml": 46.362,
        "wwl_per_unit_ml": None,
        "water_accounting_method": "unknown",
        "water_source": "static_avg",
        "weighting_methodology": "multiplier_1_plus_score, see methodology",
    },
    "embodied": {
        "co2e_kg": 0.0,
        "water_l": 0.0,
        "sku": None,
        "useful_life_hours": None,
        "source": "no_profile",
        "citation": None,
        "label": None,
    },
    "lifecycle": {
        "carbon": {
            "operational_kg": 0.006084,
            "embodied_kg": 0.0,
            "total_kg": 0.006084,
        },
        "water": {
            "operational_l": 0.034342,
            "embodied_l": 0.0,
            "total_l": 0.034342,
        },
    },
    "assumptions": {
        "region": "us-east-1",
        "region_label": "US Virginia",
        "grid_source": "static",
        "grid_profile_source": "static_avg",
        "carbon_intensity_source": "static_avg",
        "carbon_intensity_timestamp": None,
        "em_zone": None,
        "pue": 1.58,
        "pue_source": "default_iea_2024",
        "wue_direct_l_per_kwh": 0.12,
        "wue_source": "region_default",
        "grid_co2_kg_per_kwh": 0.35,
        "grid_water_l_per_kwh": 1.9,
        "water_source": "static",
        "water_accounting_method": "unknown",
        "water_stress_season": "spring",
        "water_stress_as_of": "2026-05-30T22:36:13Z",
        "cooling_type": "unknown",
        "carbon_energy_basis": "facility_kwh",
        "carbon_rationale": "Scope 2: grid intensity applied to total facility electricity (IT × PUE).",
        "cpu_tdp_fallback_w": 65.0,
        "hardware_sku": None,
        "hardware_sku_requested": None,
    },
    "caveats": CANONICAL_CAVEATS,
}

PORTFOLIO_RUN_ROW = {
    "run_dir": "/tmp/va_run_v01",
    "run_id": "va_run_v01",
    "timestamp": "2026-05-30T22:36:13+00:00",
    "timestamp_display": "2026-05-30 22:36",
    "timestamp_sort": 1717103773.0,
    "workload": "SDXL · 100 imgs",
    "workload_type": "image",
    "region": "us-east-1",
    "region_label": "US Virginia",
    "hardware": "H100 SXM 80GB",
    "hardware_sku": "h100-sxm",
    "facility_wh": 17.4,
    "carbon_g": 6.0,
    "water_ml": 31.0,
    "wwl_ml": 38.0,
    "cost_usd": 0.55,
    "units": 100,
    "unit_label": "img",
    "per_unit_wh": 0.17,
    "per_unit_carbon_g": 0.06,
    "per_unit_water_ml": 0.3,
    "per_unit_cost_usd": 0.006,
    "duration_s": 100.0,
    "samples": 100,
    "gpu_coverage_pct": 100.0,
    "gpu_measured": True,
    "cpu_measured": False,
    "is_measurement_run": True,
    "notes": None,
    "dashboard_link": "./va_run_v01/dashboard.html",
}


class TestRunSchemaContract:
    def test_canonical_summary_validates(self):
        assert validate_run_summary(CANONICAL_SUMMARY) == []

    def test_caveats_must_be_structured_objects(self):
        bad = {**CANONICAL_SUMMARY, "caveats": ["bare string caveat"]}
        errors = validate_run_summary(bad)
        assert any("bare string" in e for e in errors)

    def test_each_caveat_has_code_severity_message(self):
        for caveat in CANONICAL_SUMMARY["caveats"]:
            normalized = normalize_caveat(caveat)
            assert normalized["code"]
            assert normalized["severity"] in {"info", "warning", "error"}
            assert normalized["message"]

    def test_missing_schema_version_treated_as_0_1(self):
        summary = {k: v for k, v in CANONICAL_SUMMARY.items() if k != "schema_version"}
        version, missing = resolve_run_schema_version(summary)
        assert version == "0.1"
        assert missing is True

    def test_missing_schema_version_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            warn_missing_run_schema_version("test_run/summary.json")
        assert "missing schema_version" in buf.getvalue()
        assert "0.1" in buf.getvalue()

    def test_legacy_string_caveats_allowed_when_flag_set(self):
        legacy = {**CANONICAL_SUMMARY, "caveats": ["legacy note"]}
        assert validate_run_summary(legacy, allow_legacy_caveats=True) == []


class TestPortfolioSchemaContract:
    def test_portfolio_payload_validates(self):
        payload = build_portfolio_payload([PORTFOLIO_RUN_ROW], scan_dir="/tmp/workspace")
        assert validate_portfolio_payload(payload) == []
        assert payload["schema_version"] == "0.1-portfolio"

    def test_portfolio_schema_version_independent(self):
        payload = build_portfolio_payload([PORTFOLIO_RUN_ROW])
        assert payload["schema_version"].endswith("-portfolio")
        assert payload["schema_version"] != CANONICAL_SUMMARY["schema_version"]


class TestEmKeyWarning:
    def test_warns_when_key_set_and_grid_static(self, monkeypatch, capsys):
        monkeypatch.setenv("ELECTRICITYMAPS_API_KEY", "test-key")
        wm._warn_em_key_if_static("static")
        err = capsys.readouterr().err
        assert "ELECTRICITYMAPS_API_KEY detected" in err
        assert "--grid-source electricitymaps" in err

    def test_silent_when_grid_source_electricitymaps(self, monkeypatch, capsys):
        monkeypatch.setenv("ELECTRICITYMAPS_API_KEY", "test-key")
        wm._warn_em_key_if_static("electricitymaps")
        assert capsys.readouterr().err == ""

    def test_silent_when_no_key(self, monkeypatch, capsys):
        monkeypatch.delenv("ELECTRICITYMAPS_API_KEY", raising=False)
        wm._warn_em_key_if_static("static")
        assert capsys.readouterr().err == ""
