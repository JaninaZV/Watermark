"""Regional carbon/water comparison artifact (hero workflow)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import watermark_meter as wm
from measurement_grade import compute_measurement_grade

COMPARISON_SCHEMA_VERSION = "0.1-comparison"

WORKLOAD_REFERENCE_KWH = {
    "embeddings": {
        "it_kwh": 0.011,
        "label": "Reference embedding batch (~11 Wh IT)",
    },
    "images": {
        "it_kwh": 0.048,
        "label": "Reference SDXL batch (~48 Wh IT)",
    },
    "text": {
        "it_kwh": 0.035,
        "label": "Reference text generation batch (~35 Wh IT)",
    },
    "benchmark": {
        "it_kwh": 0.017,
        "label": "Reference 60s host benchmark (~17 Wh IT)",
    },
}


def _project_region(
    region: str,
    it_kwh: float,
    *,
    pue: float,
    cooling_type: str,
    water_source: str,
    grid_source: str,
) -> dict[str, Any]:
    run_at = wm.dt.datetime.now(wm.dt.timezone.utc)
    grid = wm.fetch_grid_profile(region, at=run_at, grid_source=grid_source)
    water_profile = wm.fetch_water_profile(
        region,
        at=run_at,
        water_source=water_source,
        cooling_type=cooling_type,
    )
    facility_kwh = it_kwh * pue
    impacts = wm.compute_impacts(it_kwh, facility_kwh, grid, water_profile)

    stub_summary = {
        "run_metadata": {"samples": 1},
        "measured_sources": {
            "cpu": ["modeled_from_util"],
            "gpu": ["no_gpu"],
            "cpu_rapl_samples": 0,
            "cpu_modeled_samples": 1,
            "gpu_missing_samples": 0,
        },
        "assumptions": {
            "region": region,
            "cooling_type": cooling_type,
            "pue_source": "user_override" if pue != wm.DEFAULT_PUE else "default_iea_2024",
            "wue_source": water_profile.get("wue_source", "region_default"),
        },
        "water": {
            "water_accounting_method": impacts["water"]["water_accounting_method"],
        },
    }
    grade, limiting = compute_measurement_grade(stub_summary, rapl_available_at_start=False)

    return {
        "region": region,
        "region_label": wm.REGION_PROFILES[region]["label"],
        "facility_wh": round(facility_kwh * 1000, 2),
        "carbon_g": round(impacts["carbon"]["co2e_kg"] * 1000, 2),
        "wwl_ml": round(impacts["water"]["wwl_ml"], 2),
        "water_ml": round(impacts["water"]["total_l"] * 1000, 2),
        "measurement_grade": grade,
        "grade_limiting_factor": limiting,
        "carbon_source": grid.get("carbon_source"),
        "water_source": impacts["water"]["water_source"],
        "water_accounting_method": impacts["water"]["water_accounting_method"],
        "stress_basin": impacts["water"].get("stress_basin"),
    }


def build_comparison(
    regions: list[str],
    *,
    workload: str,
    it_kwh: float | None,
    reference_run: Path | None,
    pue: float,
    cooling_type: str,
    water_source: str,
    grid_source: str,
) -> dict[str, Any]:
    if reference_run is not None:
        summary_path = reference_run / "summary.json" if reference_run.is_dir() else reference_run
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        it_kwh = summary["energy"]["it_total_kwh"]
        ref_label = f"reference run {reference_run}"
    elif it_kwh is not None:
        ref_label = f"explicit IT energy {it_kwh} kWh"
    elif workload in WORKLOAD_REFERENCE_KWH:
        it_kwh = WORKLOAD_REFERENCE_KWH[workload]["it_kwh"]
        ref_label = WORKLOAD_REFERENCE_KWH[workload]["label"]
    else:
        raise ValueError(
            f"unknown workload {workload!r}; pass --reference-run or --it-kwh"
        )

    per_region = [
        _project_region(
            region,
            it_kwh,
            pue=pue,
            cooling_type=cooling_type,
            water_source=water_source,
            grid_source=grid_source,
        )
        for region in regions
    ]

    by_carbon = min(per_region, key=lambda r: r["carbon_g"])
    by_wwl = min(per_region, key=lambda r: r["wwl_ml"])
    inversion = by_carbon["region"] != by_wwl["region"]

    tradeoff_parts = []
    if len(per_region) >= 2:
        high_carbon = max(per_region, key=lambda r: r["carbon_g"])
        low_carbon = by_carbon
        high_wwl = max(per_region, key=lambda r: r["wwl_ml"])
        if low_carbon["carbon_g"] > 0:
            carbon_ratio = high_carbon["carbon_g"] / low_carbon["carbon_g"]
        else:
            carbon_ratio = 1.0
        if by_wwl["wwl_ml"] > 0:
            water_ratio = high_wwl["wwl_ml"] / by_wwl["wwl_ml"]
        else:
            water_ratio = 1.0
        tradeoff_parts.append(
            f"{low_carbon['region']} is {carbon_ratio:.1f}× lower carbon than {high_carbon['region']}"
        )
        tradeoff_parts.append(
            f"{high_wwl['region']} is {water_ratio:.1f}× higher WWL than {by_wwl['region']}"
        )

    assumptions_consistent = len({r["water_source"] for r in per_region}) == 1

    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "workload": workload,
        "reference_energy": {
            "it_kwh": it_kwh,
            "description": ref_label,
            "pue": pue,
            "cooling_type": cooling_type,
            "water_source": water_source,
            "grid_source": grid_source,
        },
        "regions": per_region,
        "winners": {
            "lowest_carbon_region": by_carbon["region"],
            "lowest_wwl_region": by_wwl["region"],
            "lowest_stress_weighted_water_region": by_wwl["region"],
        },
        "water_carbon_inversion": inversion,
        "tradeoff_narrative": "; ".join(tradeoff_parts) if tradeoff_parts else None,
        "assumptions_consistent": assumptions_consistent,
    }


def compare_regions_cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compare carbon and WWL across regions for the same reference workload energy.",
    )
    p.add_argument("--workload", required=True, choices=sorted(WORKLOAD_REFERENCE_KWH.keys()))
    p.add_argument("--regions", nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--reference-run", type=Path, default=None)
    p.add_argument("--it-kwh", type=float, default=None)
    p.add_argument("--pue", type=float, default=wm.DEFAULT_PUE)
    p.add_argument("--cooling-system", default=None, dest="cooling_system")
    p.add_argument("--water-source", default="static", choices=["static", "operator"])
    p.add_argument("--grid-source", default="static", choices=["static", "electricitymaps"])
    args = p.parse_args(argv)

    for region in args.regions:
        if region not in wm.REGION_PROFILES:
            print(f"error: unknown region {region}", file=sys.stderr)
            return 2

    try:
        payload = build_comparison(
            args.regions,
            workload=args.workload,
            it_kwh=args.it_kwh,
            reference_run=args.reference_run,
            pue=args.pue,
            cooling_type=wm.normalize_cooling_type(args.cooling_system),
            water_source=args.water_source,
            grid_source=args.grid_source,
        )
    except (ValueError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[watermark] compare-regions: wrote {args.output}")
    if payload["water_carbon_inversion"]:
        print(f"[watermark] inversion: {payload['tradeoff_narrative']}")
    return 0
