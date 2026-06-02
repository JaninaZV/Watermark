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

_AUDIT_README = """# Watermark audit pack

This bundle is a self-contained, reproducible snapshot of one measurement run.

## Contents

| File | Purpose |
|------|---------|
| summary.json | Machine-readable footprint totals, grades, and provenance |
| measurements.csv | Per-sample energy readings |
| METHODOLOGY.md | Methodology document pinned to the run's schema_version |
| assumption_lineage.json | Every assumption field with source and rationale |
| caveats.json | Structured caveat list (code, severity, message) |
| README.md | This file |

## How to read this (non-technical)

- **WWL (Watershed-Weighted Liters)** is the primary water metric — gross water adjusted for basin stress.
- **measurement_grade** A/B/C reflects how much was directly measured vs modeled (see summary.json).
- **water_accounting_method** states whether numbers are consumption or withdrawal basis.
- Operator disclosures are self-reported unless disclosure_verified is true.

## Integrity

methodology_hash in summary.json is the SHA-256 of METHODOLOGY.md included here.
Re-exporting the same run_dir produces the same zip (no timestamps inside the archive).
"""


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
    meth_path = methodology_path or Path(__file__).with_name("METHODOLOGY.md")
    if not meth_path.is_file():
        raise FileNotFoundError(f"missing METHODOLOGY.md at {meth_path}")

    caveats = [normalize_caveat(c) for c in summary.get("caveats", [])]
    lineage = build_assumption_lineage(summary)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
        zf.writestr("measurements.csv", csv_path.read_text(encoding="utf-8"))
        zf.writestr("METHODOLOGY.md", meth_path.read_text(encoding="utf-8"))
        zf.writestr("assumption_lineage.json", json.dumps(lineage, indent=2, sort_keys=True) + "\n")
        zf.writestr("caveats.json", json.dumps(caveats, indent=2, sort_keys=True) + "\n")
        zf.writestr("README.md", _AUDIT_README)

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

    print(f"[watermark] audit-pack: wrote {path}")
    grade = json.loads((args.run_dir / "summary.json").read_text()).get("measurement_grade", "?")
    print(f"[watermark] measurement_grade: {grade}")
    return 0
