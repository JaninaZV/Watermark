"""Tests for per-run dashboard template polish."""

from __future__ import annotations

import json
from pathlib import Path

from dashboard import DASHBOARD_TEMPLATE, build_dashboard_payload, generate_dashboard

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

FIXTURE_WORKLOAD = {
    "run_id": "va-100sdxl-v01",
    "name": "SDXL · 100 imgs",
    "hardware": "H100 SXM 80GB",
    "image_count": 100,
    "compute_rate_usd_hr": 3.3,
}


def _write_fixture_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "va_run_v01"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps(FIXTURE_SUMMARY), encoding="utf-8")
    (run_dir / "workload.json").write_text(json.dumps(FIXTURE_WORKLOAD), encoding="utf-8")
    (run_dir / "measurements.csv").write_text(
        "interval_s,elapsed_s,cpu_watts,gpu_watts,cpu_percent,memory_percent\n"
        "1.0,1.0,10.0,100.0,50.0,30.0\n",
        encoding="utf-8",
    )
    return run_dir


def _template_html() -> str:
    return DASHBOARD_TEMPLATE.read_text(encoding="utf-8")


def _header_block(html: str) -> str:
    return html.split("<header class=\"topbar\">", 1)[1].split("</header>", 1)[0]


class TestDashboardHeaderPolish:
    def test_header_is_two_rows(self):
        header = _header_block(_template_html())
        assert header.count("topbar-main") == 1
        assert header.count("topbar-status") == 1
        assert "brand-block" not in header
        assert "brand-stack" not in header

    def test_brand_is_clickable(self):
        html = _template_html()
        assert 'class="brand-home"' in html
        assert 'id="brandHome"' in html
        assert "cursor: pointer" in html or "cursor:pointer" in html.replace(" ", "")
        assert "brand-home:hover .brand-mark" in html
        assert "scrollTo({ top: 0, behavior: 'smooth' })" in html

    def test_workspace_nav_removed(self):
        html = _template_html()
        assert "workspace-nav" not in html
        assert "ws-btn" not in html
        assert "wsWorkloadLabel" not in html
        assert "AI Footprint Lab" not in html

    def test_action_buttons_equal_width(self):
        html = _template_html()
        assert "min-width: 110px" in html
        assert "text-align: center" in html
        actions = html.split('<div class="actions">', 1)[1].split("</div>", 1)[0]
        assert 'class="action-btn primary"' not in actions
        assert actions.index("Methodology") < actions.index("Export PDF")
        assert actions.index("Export PDF") < actions.index("Share")
        assert actions.index("Share") < actions.index("Documentation")

    def test_subtitle_is_single_line_with_ellipsis(self):
        html = _template_html()
        assert 'class="header-subtitle"' in html
        assert 'id="brandSubtitle"' in html
        assert "text-overflow: ellipsis" in html
        assert "white-space: nowrap" in html
        assert "Workload footprint ·" in html
        assert "renderBrandSubtitle" in html
        assert "measured" not in html.split("function renderBrandSubtitle", 1)[1].split("function renderHeader", 1)[0]

    def test_methodology_link_present_in_actions(self):
        html = _template_html()
        assert 'href="./METHODOLOGY.md"' in html
        assert 'target="_blank"' in html
        assert ">Methodology</a>" in html

    def test_schedule_button_removed(self):
        html = _template_html()
        assert 'data-action="schedule"' not in html
        assert "⟳ Schedule" not in html

    def test_new_run_replaced_with_documentation(self):
        html = _template_html()
        assert 'href="./README.md"' in html
        assert ">Documentation</a>" in html
        assert "+ New Run" not in html
        assert 'data-action="new-run"' not in html

    def test_generate_dashboard_copies_portable_docs(self, tmp_path):
        run_dir = _write_fixture_run(tmp_path)
        generate_dashboard(
            FIXTURE_SUMMARY,
            [{"interval_s": 1.0, "elapsed_s": 1.0, "cpu_watts": 10.0, "gpu_watts": 100.0}],
            run_dir,
            workload=FIXTURE_WORKLOAD,
        )
        assert (run_dir / "dashboard.html").is_file()
        assert (run_dir / "METHODOLOGY.md").is_file()
        assert (run_dir / "README.md").is_file()
        payload = build_dashboard_payload(
            FIXTURE_SUMMARY,
            [{"interval_s": 1.0, "elapsed_s": 1.0, "cpu_watts": 10.0, "gpu_watts": 100.0}],
            workload=FIXTURE_WORKLOAD,
        )
        assert payload["run"]["timestamp_display"] == payload["run"]["started_at_display"]


class TestWorkloadMetadataCli:
    def test_workload_json_includes_model_when_flag_set(self, tmp_path):
        from dashboard import save_workload_metadata

        out = tmp_path / "meta_test"
        out.mkdir()
        path = save_workload_metadata(out, {
            "run_id": "meta-test",
            "name": "Metadata test",
            "model": "test/model-name",
            "facility_location": "Test datacenter",
            "cooling_system": "evaporative",
            "notes": "Verifying metadata CLI flags",
        })
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["model"] == "test/model-name"
        assert data["facility_location"] == "Test datacenter"
        assert data["cooling_system"] == "evaporative"
        assert data["notes"] == "Verifying metadata CLI flags"

    def test_workload_json_omits_field_when_flag_not_set(self, tmp_path):
        from dashboard import save_workload_metadata

        out = tmp_path / "meta_test2"
        out.mkdir()
        path = save_workload_metadata(out, {
            "run_id": "meta-test2",
            "name": "No metadata",
        })
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("model", "facility_location", "cooling_system", "notes"):
            assert key not in data

    def test_dashboard_sidebar_renders_experiment_metadata_section(self):
        html = _template_html()
        assert 'id="experimentMetadataSection"' in html
        assert 'id="sidebarExperimentMetadata"' in html
        assert ">Experiment metadata</h4>" in html
        block = html.split("function renderSidebar", 1)[1].split("function renderHeader", 1)[0]
        assert "metaRows" in block
        assert "w.model" in block
        assert "w.facility_location" in block
        assert "w.cooling_system" in block
        assert "w.notes" in block
        assert "Facility location" in block
        assert "Cooling system" in block

    def test_dashboard_sidebar_hides_metadata_section_when_all_absent(self):
        html = _template_html()
        block = html.split("function renderSidebar", 1)[1].split("function renderHeader", 1)[0]
        assert "metaSection.hidden = true" in block
        assert "metaRows.length" in block

    def test_gitignore_excludes_images_directories(self):
        gitignore = (Path(__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8")
        assert "**/images/" in gitignore
        assert "**/test_images/" in gitignore
        assert "**/sample_*/" in gitignore

    def test_build_dashboard_payload_includes_experiment_metadata(self):
        workload = {
            **FIXTURE_WORKLOAD,
            "model": "meta-llama/Llama-3.2-3B-Instruct",
            "facility_location": "Iceland (RunPod EUR-IS-3)",
            "cooling_system": "evaporative",
            "notes": "Text inference baseline",
        }
        payload = build_dashboard_payload(
            FIXTURE_SUMMARY,
            [{"interval_s": 1.0, "elapsed_s": 1.0, "cpu_watts": 10.0, "gpu_watts": 100.0}],
            workload=workload,
        )
        assert payload["workload"]["model"] == "meta-llama/Llama-3.2-3B-Instruct"
        assert payload["workload"]["facility_location"] == "Iceland (RunPod EUR-IS-3)"
        assert payload["workload"]["cooling_system"] == "evaporative"
        assert payload["workload"]["notes"] == "Text inference baseline"
        assert payload["run"]["facility_location"] == "Iceland (RunPod EUR-IS-3)"
