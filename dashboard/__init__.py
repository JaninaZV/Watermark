"""
Dashboard generation for Watermark meter runs.

HTML/CSS/JS live in a single template file: dashboard_template.html
(this module only injects window.WATERMARK_DATA — no duplicated markup).

Regenerate without re-measuring:
    python -m dashboard ./va_run

Preview the layout with demo data:
    open dashboard/dashboard_template.html
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
from pathlib import Path

from watermark_meter import (
    CARBON_SOURCE_EM,
    CARBON_SOURCE_STATIC,
    compute_impacts,
    fetch_water_profile,
    static_grid_profile,
    stress_badge_label,
)
from schema_contract import resolve_run_schema_version, warn_missing_run_schema_version

# Single source of truth for dashboard UI — meter injects JSON at generation time.
DASHBOARD_TEMPLATE = Path(__file__).with_name("dashboard_template.html")
DASHBOARD_COMPARE_REGIONS = ("us-east-1", "us-west-2", "eu-north-1")
DASHBOARD_DATA_MARKER = "/*__WATERMARK_DATA__*/"

REGION_CHART_LABELS = {
    "us-east-1": "Virginia (us-east-1)",
    "us-west-2": "Oregon (us-west-2)",
    "eu-north-1": "Sweden (eu-north-1)",
    "ap-northeast-1": "Japan (ap-northeast-1)",
}

# Public-source regional context — facility geography + grid characteristics.
# Sources: EPA eGRID 2022, IEA 2024, NREL Macknick 2012, hyperscaler region docs.
# Does NOT use third-party crowdsourced maps; safe to cite in published dashboards.
REGION_CONTEXT = {
    "us-east-1": {
        "facility_note": "Largest US cloud hub (AWS/Azure/GCP Northern Virginia)",
        "carbon_note": "PJM grid — moderate carbon (fossil + nuclear mix)",
        "water_note": "Humid climate; evaporative cooling + moderate grid water",
        "tension": "High AI concentration; community scrutiny on water and grid load",
    },
    "us-west-2": {
        "facility_note": "Major Pacific Northwest cloud region (Oregon)",
        "carbon_note": "Hydropower-heavy grid — among lowest US carbon intensities",
        "water_note": "Low carbon but high indirect water (hydropower evaporation)",
        "tension": "Looks 'green' on carbon; water footprint can still be large",
    },
    "eu-north-1": {
        "facility_note": "Nordic cloud region (Sweden) — popular for low-carbon claims",
        "carbon_note": "Near-zero operating carbon (hydro + nuclear dominated)",
        "water_note": "Highest indirect water in this comparison (reservoir evaporation)",
        "tension": "Cleanest carbon · often highest generation-water — the tradeoff story",
    },
}


# Annual precipitation (mm) for scale comparisons — NOAA / national climate normals.
REGION_ANNUAL_RAINFALL_MM = {
    "us-east-1": 1118,
    "us-east-2": 990,
    "us-west-1": 580,
    "us-west-2": 890,
    "eu-west-1": 930,
    "eu-west-3": 640,
    "eu-north-1": 605,
    "eu-central-1": 640,
    "ap-northeast-1": 1530,
    "ap-south-1": 2420,
    "ap-southeast-1": 2340,
    "global-avg": 800,
}

# Scale comparison constants (sources in card footnotes).
_ML_PER_CUP = 240.0
_DAILY_DRINKING_L = 2.0
_L_PER_LETTUCE_HEAD = 13.0
_G_CO2_PER_KM_CAR = 251.0  # EPA avg passenger vehicle tailpipe+upstream
_KWH_PER_PHONE_CHARGE = 0.012
_KG_CO2_PER_TREE_YEAR = 21.0  # USDA Forest Service mid-range sequestration estimate
_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_L_PER_OLYMPIC_POOL = 2_500_000.0
_NYC_LA_KM = 4500.0


def _plural(n: float, singular: str, plural: str | None = None) -> str:
    word = singular if abs(n) <= 1.0 else (plural or singular + "s")
    return word


def _format_duration_human(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def build_scale_context(impacts: dict) -> dict:
    """
    Human-scale equivalences for water, carbon, and energy — illustrative only.

    impacts keys: total_water_l, total_water_ml, total_co2_kg, total_co2_g,
                  facility_kwh, duration_s, region
    """
    total_water_l = max(impacts.get("total_water_l", 0.0), 0.0)
    total_water_ml = impacts.get("total_water_ml", total_water_l * 1000)
    total_co2_g = max(impacts.get("total_co2_g", 0.0), 0.0)
    total_co2_kg = impacts.get("total_co2_kg", total_co2_g / 1000)
    facility_kwh = max(impacts.get("facility_kwh", 0.0), 0.0)
    duration_s = max(impacts.get("duration_s", 0.0), 0.0)
    region = impacts.get("region", "global-avg")

    drinking_water_cups = total_water_ml / _ML_PER_CUP
    daily_drinking_pct = (total_water_l / _DAILY_DRINKING_L) * 100 if _DAILY_DRINKING_L else 0.0
    lettuce_equivalent = total_water_l / _L_PER_LETTUCE_HEAD if _L_PER_LETTUCE_HEAD else 0.0

    rainfall_mm = REGION_ANNUAL_RAINFALL_MM.get(region, REGION_ANNUAL_RAINFALL_MM["global-avg"])
    minutes_per_year = _SECONDS_PER_YEAR / 60
    rainfall_l_per_minute_sqm = rainfall_mm / minutes_per_year if rainfall_mm > 0 else 0.0
    rainfall_minutes_on_1sqm = (
        total_water_l / rainfall_l_per_minute_sqm if rainfall_l_per_minute_sqm > 0 else 0.0
    )

    car_meters = (total_co2_g / _G_CO2_PER_KM_CAR) * 1000 if _G_CO2_PER_KM_CAR else 0.0
    car_km = total_co2_g / _G_CO2_PER_KM_CAR if _G_CO2_PER_KM_CAR else 0.0
    phone_charges = facility_kwh / _KWH_PER_PHONE_CHARGE if _KWH_PER_PHONE_CHARGE else 0.0

    tree_seconds_to_absorb = (
        total_co2_g * _SECONDS_PER_YEAR / (_KG_CO2_PER_TREE_YEAR * 1000)
        if _KG_CO2_PER_TREE_YEAR > 0 else 0.0
    )

    year_factor = _SECONDS_PER_YEAR / duration_s if duration_s > 0 else 0.0
    annual_water_l = total_water_l * year_factor
    annual_co2_kg = total_co2_kg * year_factor
    annual_co2_g = total_co2_g * year_factor
    annual_facility_kwh = facility_kwh * year_factor
    annual_swimming_pools = annual_water_l / _L_PER_OLYMPIC_POOL if _L_PER_OLYMPIC_POOL else 0.0
    annual_car_km = annual_co2_g / _G_CO2_PER_KM_CAR if _G_CO2_PER_KM_CAR else 0.0
    nyc_la_drives = annual_car_km / _NYC_LA_KM if _NYC_LA_KM else 0.0

    cards = [
        {
            "id": "water_cups",
            "headline": (
                f"About {drinking_water_cups:.2f} {_plural(drinking_water_cups, 'cup')} "
                f"of drinking water ({total_water_ml:.0f} mL total)"
            ),
            "footnote": "USDA standard cup = 240 mL",
            "formula": f"{total_water_ml:.1f} mL ÷ 240 mL/cup = {drinking_water_cups:.3f} cups",
            "method": "total_water_ml / 240",
        },
        {
            "id": "water_daily",
            "headline": (
                f"{daily_drinking_pct:.2f}% of one person's daily drinking water "
                f"(2 L/day guideline)"
            ),
            "footnote": "WHO / US dietary reference intake ≈ 2 L/day for adults",
            "formula": f"{total_water_l:.4f} L ÷ 2.0 L × 100 = {daily_drinking_pct:.2f}%",
            "method": "total_water_l / 2.0 * 100",
        },
        {
            "id": "water_lettuce",
            "headline": (
                f"Water to grow ~{lettuce_equivalent:.2f} {_plural(lettuce_equivalent, 'head')} "
                f"of lettuce (≈13 L/head)"
            ),
            "footnote": "FAO crop water footprint benchmark for field lettuce",
            "formula": f"{total_water_l:.4f} L ÷ 13 L/head = {lettuce_equivalent:.3f} heads",
            "method": "total_water_l / 13",
        },
        {
            "id": "carbon_car",
            "headline": (
                f"Same CO₂e as an average passenger car driving {car_meters:.0f} m "
                f"({car_km:.2f} km)"
            ),
            "footnote": "EPA ≈ 251 g CO₂e/km (tailpipe + upstream, avg US passenger vehicle)",
            "formula": f"{total_co2_g:.2f} g ÷ 251 g/km = {car_km:.3f} km",
            "method": "total_co2_g / 251",
        },
        {
            "id": "carbon_phone",
            "headline": (
                f"Enough facility electricity for ~{phone_charges:.1f} "
                f"{_plural(phone_charges, 'smartphone charge')} (≈12 Wh each)"
            ),
            "footnote": "Typical 12 Wh per full smartphone charge (industry average)",
            "formula": f"{facility_kwh:.6f} kWh ÷ 0.012 kWh/charge = {phone_charges:.1f} charges",
            "method": "facility_kwh / 0.012",
        },
        {
            "id": "annual_projection",
            "headline": (
                f"If run 24/7 for a year: {annual_water_l / 1000:.1f} kL water "
                f"(≈{annual_swimming_pools:.2f} Olympic pools) and "
                f"{annual_co2_kg:.0f} kg CO₂e "
                f"(≈{nyc_la_drives:.1f}× NYC→LA drives at {int(_NYC_LA_KM)} km)"
            ),
            "footnote": (
                f"Linear extrapolation: run rate × {_SECONDS_PER_YEAR:.0f} s/yr; "
                f"pool = 2.5 ML (Olympic); NYC–LA ≈ 4500 km"
            ),
            "formula": (
                f"annual = run_total × ({_SECONDS_PER_YEAR:.0f} s ÷ {duration_s:.1f} s); "
                f"pools = {annual_water_l:.0f} L ÷ 2.5e6 L"
            ),
            "method": "run_total * (seconds_per_year / duration_s)",
        },
    ]

    return {
        "metrics": {
            "drinking_water_cups": round(drinking_water_cups, 4),
            "daily_drinking_pct": round(daily_drinking_pct, 4),
            "lettuce_equivalent": round(lettuce_equivalent, 4),
            "rainfall_minutes_on_1sqm": round(rainfall_minutes_on_1sqm, 2),
            "car_meters": round(car_meters, 2),
            "car_km": round(car_km, 4),
            "phone_charges": round(phone_charges, 2),
            "tree_seconds_to_absorb": round(tree_seconds_to_absorb, 1),
            "annual_water_l": round(annual_water_l, 2),
            "annual_co2_kg": round(annual_co2_kg, 2),
            "annual_facility_kwh": round(annual_facility_kwh, 2),
            "annual_swimming_pools": round(annual_swimming_pools, 4),
            "nyc_la_drives": round(nyc_la_drives, 2),
        },
        "cards": cards,
        "caveat": "These are scale comparisons, not offsets. See methodology for sources.",
    }


def _format_source_list(sources) -> str:
    if isinstance(sources, str):
        return sources
    return ", ".join(sources)


def _format_duration_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_utc_short(iso_ts: str) -> str:
    try:
        normalized = iso_ts.replace("Z", "+00:00")
        at = dt.datetime.fromisoformat(normalized)
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_ts[:16].replace("T", " ")


def _cpu_methodology_text(meas: dict, cpu_tdp: float) -> tuple[str, str]:
    rapl = meas.get("cpu_rapl_samples", 0)
    modeled = meas.get("cpu_modeled_samples", 0)
    total = rapl + modeled
    if rapl and not modeled:
        return (f"Intel/AMD RAPL package counters, {rapl} samples", "measured")
    if modeled and not rapl:
        return (
            f"Modeled from utilization — assumed TDP {cpu_tdp} W ({modeled}/{total} samples)",
            "modeled",
        )
    if rapl and modeled:
        return (
            f"RAPL ({rapl} samples) + utilization model ({modeled} samples, TDP {cpu_tdp} W)",
            "measured",
        )
    return ("No CPU energy recorded", "modeled")


def _gpu_methodology_text(meas: dict, interval_s: float) -> tuple[str, str]:
    gpu_sources = [s for s in meas.get("gpu", []) if s != "no_gpu"]
    if not gpu_sources:
        return ("No GPU detected", "excluded")
    mode = gpu_sources[0]
    label = {
        "nvml_pynvml": "NVIDIA NVML (pynvml)",
        "nvml_smi": "NVIDIA nvidia-smi",
    }.get(mode, mode)
    samples = meas.get("gpu_missing_samples", 0)
    sample_note = f", {samples} missing" if samples else ""
    return (f"{label}, {interval_s:g} s sampling{sample_note}", "measured")


def _carbon_methodology_text(summary: dict) -> tuple[str, str, str]:
    a = summary["assumptions"]
    source = summary["carbon"].get("source", CARBON_SOURCE_STATIC)
    intensity = a["grid_co2_kg_per_kwh"]
    region = a["region"]
    if source == CARBON_SOURCE_EM:
        ts = a.get("carbon_intensity_timestamp") or "run start"
        zone = a.get("em_zone") or region
        return (
            f"{intensity:g} kg/kWh — ElectricityMaps at {ts} (zone {zone})",
            "measured",
            f"Grid intensity: ElectricityMaps realtime ({zone}). Water still uses static regional averages.",
        )
    label = a.get("region_label", region)
    return (
        f"{intensity:g} kg/kWh — {label} annual average (static_avg)",
        "modeled",
        f"Grid intensity: EPA eGRID 2022 / IEA 2024 annual average for {label}. "
        "Use --grid-source electricitymaps for realtime carbon.",
    )


def _dashboard_warnings(summary: dict) -> tuple[list[dict], bool]:
    """Return (banner warnings, publish_safe)."""
    warnings: list[dict] = []
    publish_safe = True
    carbon_source = summary["carbon"].get("source", CARBON_SOURCE_STATIC)
    if carbon_source != CARBON_SOURCE_EM:
        return warnings, publish_safe

    publish_safe = False
    sandbox = os.environ.get("ELECTRICITYMAPS_SANDBOX", "").lower() in ("1", "true", "yes")
    if sandbox:
        warnings.append({
            "level": "critical",
            "title": "ElectricityMaps sandbox key — not for publication",
            "message": (
                "This run used an ElectricityMaps sandbox/test API key. Carbon values carry "
                "EM's sandbox disclaimer and must not be published or screenshot'd. "
                "Regenerate the dashboard from a production-key run before sharing."
            ),
        })
    else:
        warnings.append({
            "level": "warn",
            "title": "ElectricityMaps realtime carbon — verify before publishing",
            "message": (
                "Carbon intensity is tagged em_realtime from ElectricityMaps. Confirm your API "
                "key tier is production-grade before external sharing. Set ELECTRICITYMAPS_SANDBOX=1 "
                "if you know this key is sandbox-only."
            ),
        })
    return warnings, publish_safe


def _workload_warnings(workload: dict | None, summary: dict) -> list[dict]:
    """Banner notes from optional workload metadata (e.g. facility vs grid profile)."""
    if not workload:
        return []
    facility = workload.get("facility_location")
    if not facility:
        return []
    assumptions = summary.get("assumptions", {})
    region = assumptions.get("region", "")
    region_label = assumptions.get("region_label", region)
    return [{
        "level": "info",
        "title": "Grid profile vs facility location",
        "message": (
            f"The workload was measured on infrastructure in {facility}. "
            f"The analysis applies the {region_label} regional grid profile for {region} "
            "(closest match in v0.1). Nordic grids are similar but not identical — "
            "describe this as Nordic in external write-ups. Norway-specific profiles are planned for v0.2."
        ),
    }]


def _comparison_regions(measured_region: str) -> list[str]:
    """Always include the run's measured region in chart comparisons."""
    base = list(DASHBOARD_COMPARE_REGIONS)
    if measured_region not in base:
        return [measured_region, *base]
    return base


def build_regional_comparison(it_kwh: float, facility_kwh: float,
                              measured_region: str,
                              cooling_type: str = "unknown") -> list[dict]:
    """
    Same measured IT energy applied to comparison regions (static grid profiles).
    Each region uses its own vendor-published or estimated WUE for direct water.
    """
    rows = []
    for region in _comparison_regions(measured_region):
        grid_profile = static_grid_profile(region)
        water_profile = fetch_water_profile(
            region,
            cooling_type=cooling_type,
        )
        wue_direct = water_profile["wue_direct_l_per_kwh"]
        impacts = compute_impacts(
            it_kwh, facility_kwh, grid_profile, water_profile, wue_direct=wue_direct,
        )
        is_measured = region == measured_region
        ctx = REGION_CONTEXT.get(region, {})
        rows.append({
            "region": region,
            "chart_label": REGION_CHART_LABELS.get(region, grid_profile.get("label", region)),
            "short_label": grid_profile["label"].split("(")[0].strip(),
            "carbon_g": round(impacts["carbon"]["co2e_kg"] * 1000, 2),
            "water_ml": round(impacts["water"]["total_l"] * 1000, 1),
            "stress_weighted_ml": round(impacts["water"]["wwl_ml"], 1),
            "wwl_ml": round(impacts["water"]["wwl_ml"], 1),
            "direct_ml": round(impacts["water"]["direct_cooling_l"] * 1000, 1),
            "indirect_ml": round(impacts["water"]["indirect_generation_l"] * 1000, 1),
            "grid_co2": grid_profile["co2_kg_per_kwh"],
            "grid_water": water_profile["water_l_per_kwh"],
            "wue_direct_l_per_kwh": wue_direct,
            "wue_source": water_profile.get("wue_source", "region_default"),
            "stress_score": impacts["water"]["stress_score"],
            "stress_level": impacts["water"]["stress_level"],
            "stress_basin": impacts["water"]["stress_basin"],
            "stress_source": impacts["water"]["stress_source"],
            "stress_badge": stress_badge_label(impacts["water"]["stress_level"]),
            "is_measured_region": is_measured,
            "energy_source": "measured" if is_measured else "grid_scenario",
            "badge": "RUN REGION" if is_measured else "GRID SCENARIO",
            "context": ctx,
        })
    return rows


def build_dashboard_payload(summary: dict, samples: list[dict],
                            workload: dict | None = None) -> dict:
    """Build JSON payload consumed by dashboard_template.html."""
    e = summary["energy"]
    w = summary["water"]
    a = summary["assumptions"]
    rm = summary["run_metadata"]
    meas = summary["measured_sources"]

    it_kwh = e["it_total_kwh"]
    facility_kwh = e["facility_total_kwh"]
    it_wh = it_kwh * 1000
    facility_wh = facility_kwh * 1000
    cooling_wh = max(facility_wh - it_wh, 0.0)
    cpu_wh = e["it_cpu_wh"]
    gpu_wh = e["it_gpu_wh"]

    duration_s = rm["duration_s"]
    interval_s = samples[0]["interval_s"] if samples else 1.0
    avg_gpu_w = (gpu_wh / (duration_s / 3600.0)) if duration_s > 0 and gpu_wh else 0.0

    workload = workload or {}
    image_count = workload.get("image_count")
    per_image_s = (duration_s / image_count) if image_count else None

    carbon_g = summary["carbon"]["co2e_kg"] * 1000
    water_ml = w["total_l"] * 1000
    wwl_ml = w.get("wwl_ml", w.get("stress_weighted_total_l", w["total_l"]) * 1000)
    stress_weighted_ml = wwl_ml
    direct_ml = w["direct_cooling_l"] * 1000
    indirect_ml = w["indirect_generation_l"] * 1000
    wwl_per_unit_ml = w.get("wwl_per_unit_ml")
    gpu_water_fraction = w.get("gpu_water_fraction")
    measurement_grade = summary.get("measurement_grade")

    embodied = summary.get("embodied", {})
    lifecycle = summary.get("lifecycle", {})
    embodied_carbon_g = embodied.get("co2e_kg", 0.0) * 1000
    embodied_water_ml = embodied.get("water_l", 0.0) * 1000
    lifecycle_carbon_g = lifecycle.get("carbon", {}).get("total_kg", summary["carbon"]["co2e_kg"]) * 1000
    lifecycle_water_ml = lifecycle.get("water", {}).get("total_l", w["total_l"]) * 1000
    has_embodied = embodied.get("source") == "modeled"

    regional = build_regional_comparison(
        it_kwh, facility_kwh, a["region"],
        cooling_type=a.get("cooling_type", "unknown"),
    )
    measured_row = next(r for r in regional if r["is_measured_region"])
    cleanest_water = min(regional, key=lambda r: r["wwl_ml"])
    cleanest_carbon = min(regional, key=lambda r: r["carbon_g"])
    carbon_ratio = (
        measured_row["carbon_g"] / cleanest_carbon["carbon_g"]
        if cleanest_carbon["carbon_g"] > 0 else 1.0
    )
    water_ratio = (
        measured_row["wwl_ml"] / cleanest_water["wwl_ml"]
        if cleanest_water["wwl_ml"] > 0 else 1.0
    )

    cpu_method, cpu_tag = _cpu_methodology_text(meas, a["cpu_tdp_fallback_w"])
    gpu_method, gpu_tag = _gpu_methodology_text(meas, interval_s)
    carbon_method, carbon_tag, carbon_tip_source = _carbon_methodology_text(summary)

    gpu_series = [s["gpu_watts"] for s in samples if s.get("gpu_watts") is not None]
    cpu_series = [s["cpu_watts"] for s in samples if s.get("cpu_watts") is not None]
    gpu_label = "measured" if meas.get("gpu") and "no_gpu" not in meas["gpu"] else "modeled"

    compute_rate = workload.get("compute_rate_usd_hr")
    cost_usd = None
    if compute_rate is not None and duration_s > 0:
        cost_usd = round(compute_rate * (duration_s / 3600.0), 2)

    run_id = workload.get("run_id") or f"{a['region']}-{rm['samples']}s"
    warnings, publish_safe = _dashboard_warnings(summary)
    warnings.extend(_workload_warnings(workload, summary))

    scale_context = build_scale_context({
        "total_water_l": w["total_l"],
        "total_water_ml": water_ml,
        "total_co2_kg": summary["carbon"]["co2e_kg"],
        "total_co2_g": carbon_g,
        "facility_kwh": facility_kwh,
        "duration_s": duration_s,
        "region": a["region"],
    })

    return {
        "schema_version": "0.3",
        "run": {
            "id": run_id,
            "region": a["region"],
            "region_label": a["region_label"],
            "started_at_utc": rm["started_at_utc"],
            "started_at_display": _format_utc_short(rm["started_at_utc"]),
            "timestamp_display": _format_utc_short(rm["started_at_utc"]),
            "duration_s": duration_s,
            "duration_display": _format_duration_hms(duration_s),
            "samples": rm["samples"],
            "interval_s": interval_s,
            "host_os": rm["host_os"],
            "facility_location": workload.get("facility_location"),
        },
        "workload": {
            "name": workload.get("name"),
            "seed": workload.get("seed"),
            "steps": workload.get("steps"),
            "hardware": workload.get("hardware"),
            "image_count": image_count,
            "per_image_s": round(per_image_s, 2) if per_image_s is not None else None,
            "facility_location": workload.get("facility_location"),
            "model": workload.get("model"),
            "notes": workload.get("notes"),
            "cooling_system": workload.get("cooling_system"),
        },
        "kpi": {
            "facility_wh": round(facility_wh, 1),
            "carbon_g": round(carbon_g, 1),
            "carbon_operational_g": round(carbon_g, 1),
            "carbon_embodied_g": round(embodied_carbon_g, 2),
            "carbon_lifecycle_g": round(lifecycle_carbon_g, 2),
            "wwl_ml": round(wwl_ml, 1),
            "water_ml": round(water_ml, 0),
            "water_stress_weighted_ml": round(stress_weighted_ml, 0),
            "water_operational_ml": round(water_ml, 0),
            "water_embodied_ml": round(embodied_water_ml, 2),
            "water_lifecycle_ml": round(lifecycle_water_ml, 1),
            "water_direct_ml": round(direct_ml, 1),
            "water_indirect_ml": round(indirect_ml, 1),
            "gpu_avg_w": round(avg_gpu_w, 0),
            "cost_usd": cost_usd,
            "it_wh": round(it_wh, 1),
            "cooling_wh": round(cooling_wh, 1),
            "per_image_wh": round(facility_wh / image_count, 2) if image_count else None,
            "per_image_carbon_g": round(carbon_g / image_count, 2) if image_count else None,
            "per_image_wwl_ml": round(wwl_per_unit_ml, 2) if wwl_per_unit_ml is not None else (
                round(wwl_ml / image_count, 2) if image_count else None
            ),
            "per_image_water_ml": round(water_ml / image_count, 2) if image_count else None,
            "per_image_cost_usd": round(cost_usd / image_count, 3) if cost_usd and image_count else None,
            "has_embodied": has_embodied,
            "stress_basin": w.get("stress_basin"),
            "stress_level": w.get("stress_level"),
            "measurement_grade": measurement_grade,
            "gpu_water_fraction": gpu_water_fraction,
        },
        "embodied": embodied,
        "lifecycle": lifecycle,
        "energy": {
            "cpu_wh": round(cpu_wh, 1),
            "gpu_wh": round(gpu_wh, 1),
            "cooling_wh": round(cooling_wh, 1),
            "it_wh": round(it_wh, 1),
            "facility_wh": round(facility_wh, 1),
        },
        "formulas": {
            "facility": (
                f"it_total_kwh × PUE\n"
                f"= {it_kwh:.6f} × {a['pue']}\n"
                f"= {facility_kwh:.6f} kWh\n"
                f"≈ {facility_wh:.1f} Wh"
            ),
            "carbon": (
                f"facility_kWh × grid_co2_intensity\n"
                f"= {facility_kwh:.6f} × {a['grid_co2_kg_per_kwh']} kg/kWh\n"
                f"= {summary['carbon']['co2e_kg']:.6f} kg CO₂e\n"
                f"≈ {carbon_g:.1f} g"
            ),
            "water": (
                f"WWL = gross × (1 + stress)\n"
                f"direct (WUE × IT) + indirect (grid × facility)\n"
                f"= {it_kwh:.6f} × {a['wue_direct_l_per_kwh']} + "
                f"{facility_kwh:.6f} × {a['grid_water_l_per_kwh']}\n"
                f"≈ {water_ml:.0f} mL gross × (1 + {w.get('stress_score', 0)})\n"
                f"= {stress_weighted_ml:.0f} mL WWL ({w.get('stress_basin', 'basin')})"
            ),
            "gpu_avg": (
                f"it_gpu_wh ÷ duration\n"
                f"= {gpu_wh:.4f} Wh × 3600 ÷ {duration_s:.0f} s\n"
                f"≈ {avg_gpu_w:.0f} W"
            ) if gpu_wh else "No GPU energy recorded",
            "cost": (
                f"runtime × hourly rate\n"
                f"= {duration_s:.0f} s × (${compute_rate:.2f} / 3600 s)\n"
                f"≈ ${cost_usd:.2f}"
            ) if cost_usd is not None else None,
        },
        "methodology": {
            "cpu": {"text": cpu_method, "tag": cpu_tag},
            "gpu": {"text": gpu_method, "tag": gpu_tag},
            "pue": {
                "text": f"{a['pue']} — {a.get('pue_source', 'default_iea_2024')}",
                "tag": "modeled",
            },
            "carbon": {"text": carbon_method, "tag": carbon_tag},
            "wue": {
                "text": (
                    f"{a['wue_direct_l_per_kwh']} L/kWh — "
                    f"{a.get('wue_source', 'region_default')}"
                ),
                "tag": "modeled" if a.get("wue_source") == "climate_estimate" else "modeled",
            },
            "indirect_water": {"text": "NREL Macknick 2012 + USGS 2020", "tag": "modeled"},
            "grid_source": a.get("grid_source", "static"),
            "carbon_source": summary["carbon"].get("source", CARBON_SOURCE_STATIC),
        },
        "tips": {
            "facility": (
                f"IT energy from {_format_source_list(e.get('it_cpu_source', meas.get('cpu')))} (CPU) + "
                f"{_format_source_list(e.get('it_gpu_source', meas.get('gpu')))} (GPU). "
                f"PUE {a['pue']} from {a.get('pue_source', 'default_iea_2024')}."
            ),
            "carbon": carbon_tip_source,
            "water": (
                f"Direct WUE {a['wue_direct_l_per_kwh']} L/kWh IT ({a.get('wue_source', 'region_default')}). "
                f"Stress: {w.get('stress_level', 'n/a')} basin {w.get('stress_basin', 'n/a')} "
                f"({w.get('stress_source', 'wri_aqueduct_2023')}). "
                f"Indirect: NREL Macknick 2012 + USGS 2020."
                + (
                    f" GPU drove {gpu_water_fraction * 100:.0f}% of facility water this run."
                    if gpu_water_fraction is not None else ""
                )
            ),
            "embodied": (
                f"Embodied {embodied.get('label') or embodied.get('sku') or 'hardware'} "
                f"amortized over {embodied.get('useful_life_hours')} h useful life. "
                f"{embodied.get('citation') or ''}"
            ) if has_embodied else None,
            "gpu": f"{gpu_method}. {rm['samples']} samples over the run.",
            "cost": (
                f"User-supplied compute rate ${compute_rate:.2f}/hr applied to {duration_s:.0f}s runtime."
                if compute_rate is not None else None
            ),
        },
        "timeseries": {
            "gpu_watts": gpu_series,
            "cpu_watts": cpu_series,
            "gpu_label": gpu_label,
            "cpu_label": "measured" if meas.get("cpu_rapl_samples") else "modeled",
        },
        "regional_meta": {
            "measured_region": a["region"],
            "measured_label": a["region_label"],
            "headline": "Same measured energy · three grid scenarios",
            "description": (
                f"IT energy was measured once in {a['region']} ({a['region_label']}). "
                "Oregon and Sweden re-apply the same watt-hours against different regional "
                "grid carbon and water intensities — counterfactual footprints, not separate runs."
            ),
            "accountability_note": (
                "Public debate focuses on where data centers land and what they draw from local "
                "grids and watersheds. Watermark answers the next question: what did this specific "
                "workload cost — in energy, carbon, and water — with every assumption tagged."
            ),
        },
        "regional": regional,
        "regional_wue": [
            {
                "region": r["region"],
                "label": r["short_label"],
                "wue_direct_l_per_kwh": r["wue_direct_l_per_kwh"],
                "wue_source": r["wue_source"],
            }
            for r in regional
        ],
        "tradeoff": {
            "carbon_ratio_vs_cleanest": round(carbon_ratio, 1),
            "water_ratio_vs_cleanest_wwl": round(water_ratio, 1),
            "water_ratio_cleanest_vs_measured": round(
                cleanest_water["wwl_ml"] / measured_row["wwl_ml"], 1,
            ) if measured_row["wwl_ml"] > 0 else 1.0,
            "cleanest_water_region": cleanest_water["region"],
            "cleanest_carbon_region": cleanest_carbon["region"],
            "cleanest_region": cleanest_water["region"],
        },
        "next_steps": (
            "Compare regions for water–carbon tradeoffs: run the same workload in us-east-1, "
            "eu-north-1, and us-west-1, then `watermark portfolio <dir>`. "
            "You optimized for carbon. Did you check water?"
        ),
        "warnings": warnings,
        "publish_safe": publish_safe,
        "assumptions": a,
        "caveats": summary.get("caveats", []),
        "wue_direct": a["wue_direct_l_per_kwh"],
        "scope": {
            "embodied_status": embodied.get("source", "not_computed"),
            "embodied_sku": embodied.get("sku"),
            "embodied_citation": embodied.get("citation"),
            "embodied_useful_life_hours": embodied.get("useful_life_hours"),
        },
        "scale_context": scale_context,
    }


def _minimal_samples_from_summary(summary: dict) -> list[dict]:
    """Synthetic sample row when only summary.json is available for comparison."""
    rm = summary.get("run_metadata", {})
    n = max(int(rm.get("samples", 1)), 1)
    duration = float(rm.get("duration_s", 1.0))
    interval = duration / n if n else 1.0
    return [{"interval_s": interval, "elapsed_s": 0.0, "cpu_watts": 0.0, "gpu_watts": None}]


def load_compare_run(compare_path: Path) -> tuple[dict, list[dict], dict | None]:
    """
    Load a prior run for side-by-side dashboard comparison.

    Accepts a run directory or path to summary.json. Loads measurements.csv
    and workload.json from the same directory when present.
    """
    compare_path = Path(compare_path).resolve()
    if compare_path.is_dir():
        summary_path = compare_path / "summary.json"
        run_dir = compare_path
    elif compare_path.suffix == ".json":
        summary_path = compare_path
        run_dir = compare_path.parent
    else:
        raise FileNotFoundError(f"compare run not found: {compare_path}")

    if not summary_path.is_file():
        raise FileNotFoundError(f"summary not found: {summary_path}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    _, missing_version = resolve_run_schema_version(summary)
    if missing_version:
        warn_missing_run_schema_version(str(summary_path))
    for key in ("run_metadata", "energy", "carbon", "water", "assumptions"):
        if key not in summary:
            raise ValueError(f"invalid summary.json (missing {key!r}): {summary_path}")

    measurements = run_dir / "measurements.csv"
    samples = load_measurements_csv(measurements) if measurements.is_file() else _minimal_samples_from_summary(summary)

    workload_path = run_dir / "workload.json"
    workload = json.loads(workload_path.read_text(encoding="utf-8")) if workload_path.is_file() else None
    return summary, samples, workload


def _ensure_portable_docs(output_dir: Path) -> None:
    """Copy METHODOLOGY.md and README.md beside dashboard.html when available."""
    roots = [output_dir, *list(output_dir.parents), Path(__file__).resolve().parent.parent]
    for name in ("METHODOLOGY.md", "README.md"):
        dest = output_dir / name
        if dest.is_file():
            continue
        for root in roots:
            src = (root / name).resolve()
            if src.is_file() and src != dest.resolve():
                shutil.copy2(src, dest)
                break


def generate_dashboard(summary: dict, samples: list[dict], output_dir: Path,
                       workload: dict | None = None,
                       template_path: Path | None = None,
                       compare_summary: dict | None = None,
                       compare_samples: list[dict] | None = None,
                       compare_workload: dict | None = None) -> Path:
    """Write dashboard.html by injecting run data into dashboard_template.html."""
    template = template_path or DASHBOARD_TEMPLATE
    if not template.is_file():
        raise FileNotFoundError(f"dashboard template not found: {template}")

    primary = build_dashboard_payload(summary, samples, workload=workload)
    if compare_summary is not None:
        cmp_samples = compare_samples if compare_samples is not None else _minimal_samples_from_summary(compare_summary)
        comparison = build_dashboard_payload(compare_summary, cmp_samples, workload=compare_workload)
        payload = {"primary": primary, "comparison": comparison}
    else:
        payload = primary
    html = template.read_text(encoding="utf-8")
    if DASHBOARD_DATA_MARKER not in html:
        raise ValueError(f"dashboard template missing data marker {DASHBOARD_DATA_MARKER}")

    inject = f"window.WATERMARK_DATA = {json.dumps(payload, indent=2)};"
    html = html.replace(DASHBOARD_DATA_MARKER, inject, 1)

    title_run = (payload["primary"] if isinstance(payload, dict) and "primary" in payload else payload)["run"]["id"]
    html = html.replace(
        "<title>Watermark — Workload Footprint · va-100sdxl-001</title>",
        f"<title>Watermark — Workload Footprint · {title_run}</title>",
        1,
    )

    out_path = output_dir / "dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    _ensure_portable_docs(output_dir)
    return out_path


def should_write_dashboard(
    *,
    run_id: str | None = None,
    image_count: int | None = None,
    dashboard: bool = False,
    no_dashboard: bool = False,
    compare_run: str | None = None,
) -> bool:
    """
    Dashboard is off by default for quick dev runs.
    Auto-enables when experiment metadata is present (--run-id or --image-count)
    or when --compare-run is set.
    """
    if no_dashboard:
        return False
    if dashboard:
        return True
    if compare_run:
        return True
    return run_id is not None or image_count is not None


def compact_workload(workload: dict | None) -> dict | None:
    if not workload:
        return None
    compact = {k: v for k, v in workload.items() if v is not None}
    return compact or None


def save_workload_metadata(output_dir: Path, workload: dict | None) -> Path | None:
    compact = compact_workload(workload)
    if not compact:
        return None
    path = output_dir / "workload.json"
    path.write_text(json.dumps(compact, indent=2), encoding="utf-8")
    return path


def load_measurements_csv(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"measurements not found: {csv_path}")
    samples: list[dict] = []
    float_fields = ("interval_s", "elapsed_s", "cpu_watts", "gpu_watts", "cpu_percent", "memory_percent")
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sample = dict(row)
            for key in float_fields:
                if key not in sample:
                    continue
                raw = sample[key]
                if raw in (None, ""):
                    if key == "gpu_watts":
                        sample[key] = None
                    continue
                sample[key] = float(raw)
            samples.append(sample)
    if not samples:
        raise ValueError(f"no samples in {csv_path}")
    return samples


def load_run_dir(run_dir: Path) -> tuple[dict, list[dict], dict | None]:
    """Load summary.json, measurements.csv, and optional workload.json from a run directory."""
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    samples = load_measurements_csv(run_dir / "measurements.csv")
    workload_path = run_dir / "workload.json"
    workload = json.loads(workload_path.read_text(encoding="utf-8")) if workload_path.is_file() else None
    return summary, samples, workload


def merge_workload(stored: dict | None, overrides: dict) -> dict | None:
    merged = dict(stored or {})
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return compact_workload(merged)


def parse_dashboard_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        description="Generate dashboard.html from an existing Watermark run directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python -m dashboard ./va_run\n"
            "  python -m dashboard ./va_run --run-id va-100sdxl-001 --image-count 100\n"
        ),
    )
    p.add_argument("run_dir", type=Path,
                   help="Run directory with summary.json and measurements.csv.")
    p.add_argument("--output", type=Path, default=None,
                   help="Write dashboard.html here (default: run_dir/dashboard.html).")
    p.add_argument("--template", type=Path, default=None,
                   help="Dashboard HTML template (default: dashboard_template.html).")
    p.add_argument("--run-id", default=None, help="Override run id shown on the dashboard.")
    p.add_argument("--workload-name", default=None, help="Override workload label.")
    p.add_argument("--image-count", type=int, default=None, help="Override image/unit count.")
    p.add_argument("--hardware", default=None, help="Override hardware label.")
    p.add_argument("--steps", type=int, default=None, help="Override steps per unit.")
    p.add_argument("--seed", type=int, default=None, help="Override random seed.")
    p.add_argument("--compute-rate", type=float, default=None, dest="compute_rate",
                   help="Override USD/hr compute rate for cost KPI.")
    p.add_argument("--model", default=None, help="Override model ID or label.")
    p.add_argument("--facility-location", default=None, dest="facility_location",
                   help="Override physical site label.")
    p.add_argument("--notes", default=None, help="Override run notes.")
    p.add_argument("--cooling-system", default=None, dest="cooling_system",
                   help="Override facility cooling type.")
    p.add_argument("--compare-run", type=Path, default=None, dest="compare_run",
                   help="Path to another run directory (or summary.json) for side-by-side comparison.")
    args = p.parse_args(argv)
    if args.image_count is not None and args.image_count <= 0:
        p.error("--image-count must be > 0")
    if args.compute_rate is not None and args.compute_rate < 0:
        p.error("--compute-rate must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_dashboard_args(argv)
    run_dir = args.run_dir.resolve()
    summary, samples, stored_workload = load_run_dir(run_dir)
    workload = merge_workload(stored_workload, {
        "run_id": args.run_id,
        "name": args.workload_name,
        "image_count": args.image_count,
        "hardware": args.hardware,
        "steps": args.steps,
        "seed": args.seed,
        "compute_rate_usd_hr": args.compute_rate,
        "model": args.model,
        "facility_location": args.facility_location,
        "notes": args.notes,
        "cooling_system": args.cooling_system,
    })
    out_dir = (args.output or run_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    compare_summary = compare_samples = compare_workload = None
    if args.compare_run:
        compare_summary, compare_samples, compare_workload = load_compare_run(args.compare_run)
    path = generate_dashboard(
        summary, samples, out_dir,
        workload=workload,
        template_path=args.template,
        compare_summary=compare_summary,
        compare_samples=compare_samples,
        compare_workload=compare_workload,
    )
    print(f"[dashboard] wrote {path}")
    if summary.get("carbon", {}).get("source") == CARBON_SOURCE_EM:
        print("[dashboard] note: carbon is em_realtime — verify API key tier before publishing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
