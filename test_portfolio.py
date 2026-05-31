"""Tests for portfolio dashboard discovery and aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard.portfolio import (
    aggregate_portfolio_summary,
    build_insights,
    build_portfolio_payload,
    build_regional_per_unit_chart,
    compare_pair_key,
    compute_snapshot_diff,
    discover_and_load_runs,
    discover_run_dirs,
    generate_portfolio,
    generate_portfolio_compares,
    is_measurement_run,
    load_portfolio_run,
    regenerate_portfolio_after_run,
    resolve_portfolio_scan_dir,
    should_skip_dir,
    sort_portfolio_runs,
)

FIXTURE_SUMMARY = {
    "schema_version": "0.1",
    "run_metadata": {
        "started_at_utc": "2026-05-30T22:36:13+00:00",
        "duration_s": 100.0,
        "samples": 100,
        "host_os": "Linux",
    },
    "measured_sources": {"cpu": ["modeled_from_util"], "gpu": ["nvml_pynvml"], "cpu_rapl_samples": 0},
    "energy": {
        "it_cpu_wh": 1.0,
        "it_gpu_wh": 10.0,
        "it_total_kwh": 0.011,
        "facility_total_kwh": 0.01738,
    },
    "carbon": {"co2e_kg": 0.006, "source": "static_avg"},
    "water": {
        "direct_cooling_l": 0.001,
        "indirect_generation_l": 0.03,
        "total_l": 0.031,
    },
    "assumptions": {
        "region": "us-east-1",
        "region_label": "US Virginia",
        "hardware_sku": "h100-sxm",
        "pue": 1.58,
        "pue_source": "default_iea_2024",
        "wue_direct_l_per_kwh": 0.12,
        "wue_source": "region_default",
        "grid_co2_kg_per_kwh": 0.35,
        "grid_water_l_per_kwh": 1.9,
        "cpu_tdp_fallback_w": 65.0,
    },
}

DEBUG_SUMMARY = {
    **FIXTURE_SUMMARY,
    "run_metadata": {
        **FIXTURE_SUMMARY["run_metadata"],
        "duration_s": 5.0,
        "samples": 5,
    },
    "measured_sources": {
        "cpu": ["modeled_from_util"],
        "gpu": ["no_gpu"],
        "cpu_rapl_samples": 0,
        "cpu_modeled_samples": 5,
        "gpu_missing_samples": 5,
    },
    "energy": {
        "it_cpu_wh": 0.04,
        "it_gpu_wh": 0.0,
        "it_total_kwh": 0.00004,
        "facility_total_kwh": 0.000063,
    },
}


def _write_run(base: Path, name: str, *, region: str = "us-east-1", region_label: str = "US Virginia",
               started_at: str = "2026-05-30T22:36:13+00:00", workload_name: str = "SDXL · 100 imgs",
               carbon_kg: float = 0.006, water_l: float = 0.031, facility_kwh: float = 0.01738,
               summary: dict | None = None, notes: str | None = None) -> Path:
    run_dir = base / name
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(json.dumps(summary or FIXTURE_SUMMARY))
    payload["run_metadata"]["started_at_utc"] = started_at
    payload["assumptions"]["region"] = region
    payload["assumptions"]["region_label"] = region_label
    payload["carbon"]["co2e_kg"] = carbon_kg
    payload["water"]["total_l"] = water_l
    payload["energy"]["facility_total_kwh"] = facility_kwh
    (run_dir / "summary.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "measurements.csv").write_text(
        "interval_s,elapsed_s,cpu_watts,gpu_watts,cpu_percent,memory_percent\n"
        "1.0,1.0,10.0,100.0,50.0,30.0\n",
        encoding="utf-8",
    )
    workload_payload = {
        "run_id": name,
        "name": workload_name,
        "image_count": 100,
        "hardware": "H100 SXM 80GB",
        "compute_rate_usd_hr": 3.3,
    }
    if notes:
        workload_payload["notes"] = notes
    (run_dir / "workload.json").write_text(json.dumps(workload_payload), encoding="utf-8")
    return run_dir


def _write_debug_run(base: Path, name: str = "verify_test") -> Path:
    return _write_run(
        base, name,
        summary=DEBUG_SUMMARY,
        facility_kwh=0.000063,
        carbon_kg=0.00002,
        water_l=0.0001,
        workload_name="debug stub",
    )


class TestPortfolioDiscovery:
    def test_portfolio_discovers_all_runs(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        _write_run(tmp_path, "se_run_v01", region="eu-north-1", region_label="Sweden",
                   started_at="2026-05-31T01:42:45+00:00", workload_name="SDXL · Nordic")
        found = discover_run_dirs(tmp_path)
        assert {p.name for p in found} == {"va_run_v01", "se_run_v01"}
        runs = discover_and_load_runs(tmp_path)
        assert len(runs) == 2
        assert {r["run_id"] for r in runs} == {"va_run_v01", "se_run_v01"}

    def test_portfolio_skips_venv_and_build(self, tmp_path):
        _write_run(tmp_path, "good_run")
        venv = tmp_path / "venv" / "lib" / "hidden_run"
        venv.mkdir(parents=True)
        (venv / "summary.json").write_text(json.dumps(FIXTURE_SUMMARY), encoding="utf-8")
        build = tmp_path / "build" / "bad_run"
        build.mkdir(parents=True)
        (build / "summary.json").write_text(json.dumps(FIXTURE_SUMMARY), encoding="utf-8")
        dot = tmp_path / ".hidden"
        dot.mkdir()
        (dot / "summary.json").write_text(json.dumps(FIXTURE_SUMMARY), encoding="utf-8")
        underscore = tmp_path / "_cache" / "run"
        underscore.mkdir(parents=True)
        (underscore / "summary.json").write_text(json.dumps(FIXTURE_SUMMARY), encoding="utf-8")
        found = discover_run_dirs(tmp_path)
        assert [p.name for p in found] == ["good_run"]
        assert should_skip_dir("venv")
        assert should_skip_dir(".git")
        assert should_skip_dir("_pytest_cache")

    def test_portfolio_sorts_correctly(self, tmp_path):
        _write_run(tmp_path, "old_run", started_at="2026-05-01T10:00:00+00:00", carbon_kg=0.010)
        _write_run(tmp_path, "new_run", started_at="2026-06-01T10:00:00+00:00", carbon_kg=0.002)
        runs = discover_and_load_runs(tmp_path)
        by_ts = sort_portfolio_runs(runs, "timestamp")
        assert [r["run_id"] for r in by_ts] == ["new_run", "old_run"]
        by_carbon = sort_portfolio_runs(runs, "carbon")
        assert [r["run_id"] for r in by_carbon] == ["old_run", "new_run"]

    def test_portfolio_aggregates_totals(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        _write_run(tmp_path, "se_run_v01", region="eu-north-1", region_label="Sweden",
                   started_at="2026-05-31T01:42:45+00:00", facility_kwh=0.0508,
                   carbon_kg=0.002, water_l=0.120)
        runs = discover_and_load_runs(tmp_path)
        summary = aggregate_portfolio_summary([r for r in runs if r["is_measurement_run"]])
        assert summary["run_count"] == 2
        assert summary["total_facility_wh"] == pytest.approx(98.8, abs=0.2)
        assert summary["total_carbon_g"] == pytest.approx(18.8, abs=0.2)
        assert summary["total_water_ml"] == pytest.approx(215.0, abs=1.0)
        assert set(summary["regions"]) == {"us-east-1", "eu-north-1"}
        assert summary["date_range"]["earliest"] == "2026-05-30"
        assert summary["date_range"]["latest"] == "2026-05-31"

    def test_portfolio_excludes_non_measurement_runs_from_totals(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        _write_debug_run(tmp_path, "verify_test")
        runs = discover_and_load_runs(tmp_path)
        payload = build_portfolio_payload(runs)
        assert len(payload["runs"]) == 2
        assert payload["summary"]["run_count"] == 1
        assert payload["summary"]["total_facility_wh"] == pytest.approx(48.0, abs=0.5)
        assert len(payload["regional_chart"]) == 1
        assert not load_portfolio_run(tmp_path / "verify_test")["is_measurement_run"]
        assert is_measurement_run(False, 0.063, 5.0) is False

    def test_portfolio_table_includes_per_unit_columns(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        out = tmp_path / "portfolio.html"
        html = generate_portfolio(tmp_path, out).read_text(encoding="utf-8")
        assert "Wh/unit" in html
        assert "g CO₂e/unit" in html
        assert "mL/unit" in html
        assert "$/unit" in html
        assert '"per_unit_wh"' in html
        assert "col-per-unit" in html

    def test_portfolio_gpu_indicator_present_per_row(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        _write_debug_run(tmp_path, "verify_test")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert "MEASURED" in html
        assert "MODELED" in html
        assert "source-pill measured" in html
        assert "source-pill modeled" in html

    def test_portfolio_brand_matches_per_run_dashboard_styling(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert 'WATER<span class="brand-mark">MARK</span>' in html
        assert 'class="version-pill">v0.1</span>' in html
        assert "Methodology →" in html
        assert '"methodology_version": "0.1.0"' in html
        assert "Generated" in html
        assert 'id="hdrGeneratedIso"' in html
        assert "topbar-status" in html
        assert 'id="snapshotTime"' in html
        assert "Snapshot · generated" in html
        assert "summaryMeta" in html
        assert "formatRunTimestamp" in html
        assert "formatLocal" in html
        assert "formatUtc" in html
        assert "initPortfolio" in html
        assert 'data-sort="run_id"' in html
        assert "Show debug/test runs" in html
        assert 'id="showDebugRuns" checked' in html
        assert "Per-unit footprint by workload (filtered to measurement runs)" in html

    def test_portfolio_insights_and_executive_summary(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        _write_run(
            tmp_path, "se_run_v01", region="eu-north-1", region_label="Sweden",
            started_at="2026-05-31T01:42:45+00:00", facility_kwh=0.0508,
            carbon_kg=0.002, water_l=0.120, workload_name="SDXL · Nordic",
        )
        runs = discover_and_load_runs(tmp_path)
        payload = build_portfolio_payload(runs)
        assert payload["executive_summary"]
        assert payload["insights"]
        assert payload["attestation"]["data_fingerprint"]
        assert payload["cost_carbon_chart"]
        regional = [i for i in payload["insights"] if i["type"] == "regional"]
        assert regional
        assert "×" in regional[0]["text"]

    def test_portfolio_snapshot_sidecar(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        out = tmp_path / "portfolio.html"
        generate_portfolio(tmp_path, out)
        snap = tmp_path / "portfolio.snapshot.json"
        assert snap.is_file()
        generate_portfolio(tmp_path, out)
        html = out.read_text(encoding="utf-8")
        assert '"snapshot_diff"' in html

    def test_snapshot_diff_detects_new_runs(self):
        prev = {
            "generated_at_utc": "2026-05-30T00:00:00Z",
            "measurement_run_ids": ["va_run_v01"],
            "data_fingerprint": "abc",
            "summary": {"total_carbon_g": 10.0},
        }
        diff = compute_snapshot_diff(
            prev,
            {"total_carbon_g": 18.8},
            ["va_run_v01", "se_run_v01"],
            "def",
        )
        assert diff["new_measurement_run_ids"] == ["se_run_v01"]
        assert diff["carbon_delta_g"] == pytest.approx(8.8)

    def test_portfolio_v02_finish_trio(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", facility_kwh=0.048, carbon_kg=0.0168, water_l=0.095)
        _write_run(
            tmp_path, "se_run_v01", region="eu-north-1", region_label="Sweden",
            started_at="2026-05-31T01:42:45+00:00", facility_kwh=0.0508,
            carbon_kg=0.002, water_l=0.120, workload_name="SDXL · Nordic",
        )
        runs = discover_and_load_runs(tmp_path)
        measurement = [r for r in runs if r["is_measurement_run"]]
        per_unit = build_regional_per_unit_chart(measurement)
        assert len(per_unit) == 2
        assert all(r["per_unit_carbon_g"] > 0 for r in per_unit)

        out = tmp_path / "portfolio.html"
        generate_portfolio(tmp_path, out)
        html = out.read_text(encoding="utf-8")
        assert "regional_per_unit_chart" in html
        assert "compare_links" in html
        assert "filter_run_ids" in html
        assert 'id="regionPerUnitChart"' in html
        assert 'id="compareSelectedBtn"' in html
        assert "filterToRunIds" in html
        assert "openCompareForSelection" in html

        key = compare_pair_key("va_run_v01", "se_run_v01")
        compare_dir = tmp_path / "portfolio_compare"
        assert compare_dir.is_dir()
        assert any(compare_dir.iterdir())
        payload = build_portfolio_payload(runs)
        links = generate_portfolio_compares(measurement, out, payload["insights"])
        assert key in links
        compare_html = tmp_path / links[key]
        assert compare_html.is_file()
        assert "comparison" in compare_html.read_text(encoding="utf-8")

    def test_portfolio_v02_ui_features(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert "Executive summary" in html
        assert 'id="exportCsvBtn"' in html
        assert "Board PDF" in html
        assert "renderExecutive" in html
        assert "exportCsv" in html
        assert "syncUrlState" in html
        assert "costCarbonChart" in html
        assert "healthBadge" in html
        assert "opened just now" in html

    def test_portfolio_export_pdf_and_brand_home(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert 'id="exportPdfBtn"' in html
        assert "Board PDF" in html
        assert 'id="brandHome"' in html
        assert "goPortfolioHome" in html
        assert "window.print()" in html
        assert "@media print" in html
        assert 'id="toast"' in html

    def test_portfolio_chart_drill_down(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert 'id="drillDownBar"' in html
        assert "setDrillDown" in html
        assert "Click a bar to filter the runs table by region" in html
        assert "Click a bar to filter the runs table by workload" in html
        assert '"portfolio_href"' in html

    def test_generate_portfolio_html(self, tmp_path):
        root = tmp_path / "project"
        _write_run(root, "va_run_v01")
        _write_run(root, "se_run_v01", region="eu-north-1", region_label="Sweden",
                   started_at="2026-05-31T01:42:45+00:00")
        out = tmp_path / "portfolio.html"
        path = generate_portfolio(root, out)
        html = path.read_text(encoding="utf-8")
        assert "va_run_v01" in html
        assert "se_run_v01" in html
        assert "WATERMARK_PORTFOLIO_DATA" in html
        assert "Portfolio summary" in html

    def test_portfolio_v02_finish_trio(self, tmp_path):
        _write_run(tmp_path, "va_run_v01", notes="Baseline Virginia run")
        _write_run(tmp_path, "se_run_v01", region="eu-north-1", region_label="Sweden",
                   started_at="2026-05-31T01:42:45+00:00", notes="Nordic comparison")
        out = tmp_path / "portfolio.html"
        generate_portfolio(tmp_path, out)
        runs = discover_and_load_runs(tmp_path)
        notes = {r["run_id"]: r.get("notes") for r in runs}
        assert notes["va_run_v01"] == "Baseline Virginia run"
        assert notes["se_run_v01"] == "Nordic comparison"
        html = out.read_text(encoding="utf-8")
        assert 'id="sectionNav"' in html
        assert 'href="#execPanel"' in html
        assert 'href="#chartsPanel"' in html
        assert "initSectionNav" in html
        assert 'class="col-notes"' in html
        assert 'id="emptyStatePanel"' in html
        assert "renderEmptyState" in html
        assert 'id="regenerateBanner"' not in html

    def test_portfolio_empty_state_shows_with_one_run(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert "One measurement run — add another to compare regions" in html
        assert 'id="emptyStatePanel"' in html

    def test_all_chart_panels_full_width_in_grid(self, tmp_path):
        _write_run(tmp_path, "va_run_v01")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert ".charts-grid.three-col" in html
        assert "grid-template-columns: 1fr;" in html
        charts = html.split('id="chartsPanel"', 1)[1].split('<div class="toast"', 1)[0]
        for panel_id in (
            "regionPerUnitPanel",
            "regionPanel",
            "workloadPanel",
            "costCarbonPanel",
            "trendPanel",
        ):
            assert f'panel chart-full" id="{panel_id}"' in charts

    def test_chart_box_height_increased(self):
        template = Path(__file__).resolve().parent / "dashboard" / "portfolio_template.html"
        html = template.read_text(encoding="utf-8")
        assert ".chart-box { height: 320px;" in html

    def test_print_chart_box_height_unchanged(self):
        template = Path(__file__).resolve().parent / "dashboard" / "portfolio_template.html"
        html = template.read_text(encoding="utf-8")
        assert ".chart-box { height: 220px !important; }" in html

    def test_search_filters_runs_by_substring(self):
        html = (Path(__file__).resolve().parent / "dashboard" / "portfolio_template.html").read_text(encoding="utf-8")
        assert 'id="runSearch"' in html
        assert "Search runs" in html
        assert "matchesRunSearch" in html
        assert "run.region_label" in html
        assert 'id="runsNoResults"' in html

    def test_export_shown_only_includes_visible_rows(self):
        html = (Path(__file__).resolve().parent / "dashboard" / "portfolio_template.html").read_text(encoding="utf-8")
        assert 'id="exportShownBtn"' in html
        assert "exportShownCsv" in html
        assert "getVisibleRuns" in html
        assert "updateExportButtons" in html
        assert "hasActiveTableFilters" in html
        assert "exportCsvRows(filteredRuns" in html
        assert "exportCsvRows(visibleRuns" in html

    def test_filter_dropdowns_show_counts(self):
        html = (Path(__file__).resolve().parent / "dashboard" / "portfolio_template.html").read_text(encoding="utf-8")
        assert "formatFilterOption" in html
        assert "countRunsBy" in html
        assert "`${value} (${count})`" in html

    def test_time_range_filter_excludes_older_runs(self, tmp_path):
        _write_run(tmp_path, "old_run", started_at="2025-01-01T10:00:00+00:00")
        _write_run(tmp_path, "new_run", started_at="2026-06-01T10:00:00+00:00")
        html = generate_portfolio(tmp_path, tmp_path / "portfolio.html").read_text(encoding="utf-8")
        assert 'id="filterTimeRange"' in html
        assert "passesTimeRange" in html
        assert "initTimeRangeFilter" in html
        assert 'id="timeRange7d"' in html
        runs = discover_and_load_runs(tmp_path)
        ts = {r["run_id"]: r["timestamp_sort"] for r in runs}
        assert ts["new_run"] > ts["old_run"]
        assert (ts["new_run"] - ts["old_run"]) / 86400 > 7


class TestPortfolioAutoRegen:
    def test_resolve_scan_dir_from_parent_with_portfolio(self, tmp_path):
        workspace = tmp_path / "workspace"
        run_dir = workspace / "india_run_v01"
        run_dir.mkdir(parents=True)
        (workspace / "portfolio.html").write_text("<html></html>", encoding="utf-8")
        assert resolve_portfolio_scan_dir(run_dir) == workspace.resolve()

    def test_resolve_scan_dir_from_parent_with_runs(self, tmp_path):
        workspace = tmp_path / "workspace"
        _write_run(workspace, "va_run_v01")
        run_dir = workspace / "india_run_v01"
        run_dir.mkdir()
        assert resolve_portfolio_scan_dir(run_dir) == workspace.resolve()

    def test_resolve_scan_dir_explicit_portfolio_dir(self, tmp_path):
        run_dir = tmp_path / "orphan_run"
        run_dir.mkdir()
        explicit = tmp_path / "custom_scan"
        explicit.mkdir()
        assert resolve_portfolio_scan_dir(run_dir, portfolio_dir=explicit) == explicit.resolve()

    def test_resolve_scan_dir_returns_none_for_isolated_run(self, tmp_path):
        run_dir = tmp_path / "solo_run"
        run_dir.mkdir()
        assert resolve_portfolio_scan_dir(run_dir) is None

    def test_regenerate_after_run_writes_portfolio(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        _write_run(workspace, "va_run_v01", region="us-east-1")
        india = workspace / "india_run_v01"
        _write_run(india, "india_run_v01", region="ap-south-1", started_at="2026-06-02T10:00:00+00:00")
        path = regenerate_portfolio_after_run(india)
        assert path == (workspace / "portfolio.html").resolve()
        html = path.read_text(encoding="utf-8")
        assert "india_run_v01" in html or "ap-south-1" in html
        assert len(discover_run_dirs(workspace)) == 2
