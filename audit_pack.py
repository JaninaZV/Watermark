"""Reproducible audit-pack export for compliance review."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from schema_contract import normalize_caveat
from workload_metrics import PRE_ANNOTATION_SUMMARY_FILENAME, is_post_measurement_normalization

AUDIT_PACK_SCHEMA_VERSION = "1.0"

_AUDIT_README_BASE = """# Watermark audit pack

This bundle is a self-contained, reproducible snapshot of one measurement run.

## Contents

| File | Purpose |
|------|---------|
| audit_manifest.json | Bundle metadata including post-measurement normalization flag |
| summary.json | Current footprint totals, grades, and provenance |
{extra_summary_rows}| measurements.csv | Per-sample energy readings |
| METHODOLOGY.md | Methodology document pinned to the run's schema_version |
| assumption_lineage.json | Every assumption field with source and rationale |
| caveats.json | Structured caveat list (code, severity, message) |
| README.md | This file |

## How to read this (non-technical)

- **WWL (Watershed-Weighted Liters)** is the primary water metric — gross water adjusted for basin stress.
- **measurement_grade** A/B/C reflects how much was directly measured vs modeled (see summary.json).
- **water_accounting_method** states whether numbers are consumption or withdrawal basis.
- Operator disclosures are self-reported unless disclosure_verified is true.
{normalization_section}
## Integrity

methodology_hash in summary.json is the SHA-256 of METHODOLOGY.md included here.
Re-exporting the same run_dir produces the same zip (no timestamps inside the archive).
"""

_NORMALIZATION_SECTION_NONE = """\
- **normalization_applied_post_measurement** is `false` — any per_unit figures in summary.json
  were computed at measurement time (CLI flags or workload_metrics.json during the run).
"""

_NORMALIZATION_SECTION_POST_HOC = """\
- **normalization_applied_post_measurement** is `true` — per_unit figures in summary.json
  were applied **after** the original measurement via `watermark annotate`.
- **summary.pre_annotation.json** is the measurement-time artifact (totals and grades as recorded
  at run end, **without** post-hoc token/request normalization). Use it for audit of measured
  energy/water/carbon totals; use summary.json for per-unit CSRD-style intensity metrics.
- For CSRD and compliance review, treat these as **distinct artifacts** — do not substitute
  the annotated summary for the pre-annotation file when attesting to measurement-time totals.
"""


def build_audit_readme(*, post_hoc: bool) -> str:
    extra_rows = ""
    if post_hoc:
        extra_rows = (
            "| summary.pre_annotation.json | Measurement-time summary before annotate |\n"
        )
    normalization_section = (
        _NORMALIZATION_SECTION_POST_HOC if post_hoc else _NORMALIZATION_SECTION_NONE
    )
    return _AUDIT_README_BASE.format(
        extra_summary_rows=extra_rows,
        normalization_section=normalization_section,
    )


def build_audit_manifest(
    *,
    post_hoc: bool,
    has_pre_annotation: bool,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "audit_pack_schema_version": AUDIT_PACK_SCHEMA_VERSION,
        "normalization_applied_post_measurement": post_hoc,
    }
    artifacts: dict[str, str] = {
        "summary.json": (
            "current run summary (includes post-hoc per_unit when normalization_applied_post_measurement is true)"
            if post_hoc
            else "measurement-time run summary"
        ),
    }
    if post_hoc:
        if has_pre_annotation:
            artifacts[PRE_ANNOTATION_SUMMARY_FILENAME] = (
                "measurement-time summary preserved before watermark annotate"
            )
        else:
            manifest["pre_annotation_summary_missing"] = (
                "summary flagged post-hoc normalization but summary.pre_annotation.json not found in run_dir"
            )
    manifest["artifacts"] = artifacts
    return manifest


def methodology_hash(methodology_path: Path | None = None) -> str:
    path = methodology_path or Path(__file__).with_name("METHODOLOGY.md")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def build_assumption_lineage(summary: dict[str, Any]) -> dict[str, Any]:
    assumptions = summary.get("assumptions", {})
    water = summary.get("water", {})
    carbon = summary.get("carbon", {})
    energy = summary.get("energy", {})
    lines: list[dict[str, Any]] = []

    def add(field: str, value: Any, source: str, rationale: str) -> None:
        lines.append({
            "field": field,
            "value": value,
            "source": source,
            "rationale": rationale,
        })

    add("region", assumptions.get("region"), "cli --region", "Grid and water intensity lookup key")
    add("pue", assumptions.get("pue"), assumptions.get("pue_source"), "Facility energy = IT × PUE")
    add(
        "wue_direct_l_per_kwh",
        assumptions.get("wue_direct_l_per_kwh"),
        assumptions.get("wue_source"),
        "Direct cooling water per kWh IT",
    )
    add(
        "grid_co2_kg_per_kwh",
        assumptions.get("grid_co2_kg_per_kwh"),
        assumptions.get("carbon_intensity_source"),
        "Operational carbon intensity",
    )
    add(
        "grid_water_l_per_kwh",
        assumptions.get("grid_water_l_per_kwh"),
        assumptions.get("water_source"),
        "Indirect generation water per kWh facility",
    )
    add(
        "cooling_type",
        assumptions.get("cooling_type"),
        "cli --cooling-system",
        "Adjusts direct WUE multiplier",
    )
    add(
        "water_accounting_method",
        water.get("water_accounting_method"),
        assumptions.get("water_source"),
        "Consumption vs withdrawal basis for water totals",
    )
    add(
        "water_stress_season",
        assumptions.get("water_stress_season"),
        assumptions.get("stress_source", "wri_aqueduct_2023"),
        "Seasonal basin stress multiplier",
    )
    if assumptions.get("operator_disclosure_source"):
        add(
            "operator_disclosure_source",
            assumptions.get("operator_disclosure_source"),
            "operator_disclosures.json",
            "Self-reported operator WUE disclosure",
        )
        add(
            "disclosure_verified",
            assumptions.get("disclosure_verified"),
            "operator_disclosures.json",
            "Independent verification flag (default false)",
        )
    add("facility_total_kwh", energy.get("facility_total_kwh"), energy.get("facility_source"), "Scope 2 energy basis")
    add("co2e_kg", carbon.get("co2e_kg"), carbon.get("source"), assumptions.get("carbon_rationale", ""))
    add("wwl_ml", water.get("wwl_ml"), water.get("water_source"), water.get("weighting_methodology", ""))
    add("measurement_grade", summary.get("measurement_grade"), "measurement_grade.py", "Worst-condition rollup")
    add(
        "grade_limiting_factor",
        summary.get("grade_limiting_factor"),
        "measurement_grade.py",
        "Machine-readable grade limiter",
    )
    add(
        "normalization_applied_post_measurement",
        summary.get("normalization_applied_post_measurement"),
        "summary.json",
        "True when per_unit was applied via watermark annotate after measurement",
    )
    add(
        "methodology_hash",
        summary.get("run_metadata", {}).get("methodology_hash"),
        "METHODOLOGY.md",
        "Pinned methodology document digest",
    )
    return {"assumptions": lines}


def create_audit_pack(run_dir: Path, output: Path, methodology_path: Path | None = None) -> Path:
    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.json"
    csv_path = run_dir / "measurements.csv"
    if not summary_path.is_file():
        raise FileNotFoundError(f"missing summary.json in {run_dir}")
    if not csv_path.is_file():
        raise FileNotFoundError(f"missing measurements.csv in {run_dir}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    post_hoc = is_post_measurement_normalization(summary)
    pre_annotation_path = run_dir / PRE_ANNOTATION_SUMMARY_FILENAME
    has_pre_annotation = pre_annotation_path.is_file()

    meth_path = methodology_path or Path(__file__).with_name("METHODOLOGY.md")
    if not meth_path.is_file():
        raise FileNotFoundError(f"missing METHODOLOGY.md at {meth_path}")

    caveats = [normalize_caveat(c) for c in summary.get("caveats", [])]
    lineage = build_assumption_lineage(summary)
    manifest = build_audit_manifest(post_hoc=post_hoc, has_pre_annotation=has_pre_annotation)
    readme = build_audit_readme(post_hoc=post_hoc)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("audit_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        zf.writestr("summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
        if has_pre_annotation:
            pre_summary = json.loads(pre_annotation_path.read_text(encoding="utf-8"))
            zf.writestr(
                PRE_ANNOTATION_SUMMARY_FILENAME,
                json.dumps(pre_summary, indent=2, sort_keys=True) + "\n",
            )
        zf.writestr("measurements.csv", csv_path.read_text(encoding="utf-8"))
        zf.writestr("METHODOLOGY.md", meth_path.read_text(encoding="utf-8"))
        zf.writestr("assumption_lineage.json", json.dumps(lineage, indent=2, sort_keys=True) + "\n")
        zf.writestr("caveats.json", json.dumps(caveats, indent=2, sort_keys=True) + "\n")
        zf.writestr("README.md", readme)

    return output


def audit_pack_cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export an immutable audit pack zip for a measurement run.")
    p.add_argument("run_dir", type=Path, help="Run directory containing summary.json and measurements.csv")
    p.add_argument("--output", "-o", type=Path, required=True, help="Output audit.zip path")
    p.add_argument("--methodology", type=Path, default=None, help="Override METHODOLOGY.md path")
    args = p.parse_args(argv)

    try:
        path = create_audit_pack(args.run_dir, args.output, args.methodology)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    post_hoc = is_post_measurement_normalization(summary)
    print(f"[watermark] audit-pack: wrote {path}")
    print(f"[watermark] measurement_grade: {summary.get('measurement_grade', '?')}")
    print(f"[watermark] normalization_applied_post_measurement: {post_hoc}")
    if post_hoc:
        pre = args.run_dir / PRE_ANNOTATION_SUMMARY_FILENAME
        if pre.is_file():
            print(f"[watermark] included measurement-time artifact: {PRE_ANNOTATION_SUMMARY_FILENAME}")
        else:
            print(
                "[watermark] warning: post-hoc normalization flagged but "
                f"{PRE_ANNOTATION_SUMMARY_FILENAME} not found",
                file=sys.stderr,
            )
    return 0
