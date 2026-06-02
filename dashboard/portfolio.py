"""Portfolio dashboard — aggregate many runs into one HTML view."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from schema_contract import resolve_run_schema_version, warn_missing_run_schema_version

PORTFOLIO_TEMPLATE = Path(__file__).with_name("portfolio_template.html")
PORTFOLIO_DATA_MARKER = "/*__WATERMARK_PORTFOLIO_DATA__*/"

SKIP_DIR_NAMES = frozenset({
    "venv", "build", "__pycache__", "node_modules", ".git", ".pytest_cache",
    "site-packages", "dist", "egg-info",
})
SKIP_DIR_PREFIXES = (".", "_")

SORT_CHOICES = ("timestamp", "region", "workload", "carbon", "water", "wwl", "cost", "energy")
METHODOLOGY_VERSION = "0.1.0"


def is_measurement_run(gpu_measured: bool, facility_wh: float, duration_s: float) -> bool:
    """True when the run looks like a real workload measurement, not a debug stub."""
    return bool(gpu_measured or facility_wh >= 1.0 or duration_s >= 30)


def should_skip_dir(name: str) -> bool:
    if name in SKIP_DIR_NAMES:
        return True
    if name.endswith(".egg-info"):
        return True
    return name.startswith(SKIP_DIR_PREFIXES)


def discover_run_dirs(root: Path) -> list[Path]:
    """Recursively find directories containing summary.json."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"portfolio scan directory not found: {root}")

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        if "summary.json" in filenames:
            found.append(Path(dirpath))
    return found


def _parse_ts(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _infer_workload_type(name: str | None, run_id: str | None) -> str:
    text = f"{name or ''} {run_id or ''}".lower()
    if "sdxl" in text or "image" in text or "img" in text:
        return "images"
    if "embed" in text:
        return "embeddings"
    if "code" in text or "starcoder" in text:
        return "code"
    if "text" in text or "llama" in text or "completion" in text:
        return "text"
    return "other"


def _unit_label(workload_type: str) -> str:
    return {
        "images": "image",
        "text": "completion",
        "code": "completion",
        "embeddings": "embedding",
    }.get(workload_type, "unit")


def _region_display(summary: dict, workload: dict | None) -> str:
    workload = workload or {}
    facility = workload.get("facility_location")
    a = summary.get("assumptions", {})
    grid = a.get("region_label") or a.get("region", "")
    if facility:
        return f"{facility} · {grid}"
    return grid


def _run_cost(summary: dict, workload: dict | None) -> float | None:
    workload = workload or {}
    rate = workload.get("compute_rate_usd_hr")
    if rate is None:
        return None
    duration_s = summary.get("run_metadata", {}).get("duration_s", 0.0)
    if duration_s <= 0:
        return None
    return round(rate * (duration_s / 3600.0), 2)


def _relative_dashboard_link(output_file: Path, run_dir: Path) -> str | None:
    dash = run_dir / "dashboard.html"
    if not dash.is_file():
        return None
    try:
        return os.path.relpath(dash, output_file.parent)
    except ValueError:
        return str(dash)


def load_portfolio_run(run_dir: Path) -> dict[str, Any]:
    """Load one run directory into a portfolio row dict."""
    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    _, missing_version = resolve_run_schema_version(summary)
    if missing_version:
        warn_missing_run_schema_version(str(summary_path))
    for key in ("run_metadata", "energy", "carbon", "water", "assumptions"):
        if key not in summary:
            raise ValueError(f"invalid summary.json (missing {key!r}): {summary_path}")

    workload_path = run_dir / "workload.json"
    workload = (
        json.loads(workload_path.read_text(encoding="utf-8"))
        if workload_path.is_file() else None
    )
    workload = workload or {}

    rm = summary["run_metadata"]
    a = summary["assumptions"]
    e = summary["energy"]
    facility_wh = e["facility_total_kwh"] * 1000
    carbon_g = summary["carbon"]["co2e_kg"] * 1000
    water_ml = summary["water"]["total_l"] * 1000
    wwl_ml = summary["water"].get("wwl_ml", summary["water"].get("stress_weighted_total_l", summary["water"]["total_l"]) * 1000)
    cost_usd = _run_cost(summary, workload)
    image_count = workload.get("image_count")
    units = image_count or 1
    workload_name = workload.get("name") or workload.get("run_id") or run_dir.name
    run_id = workload.get("run_id") or run_dir.name
    workload_type = _infer_workload_type(workload_name, run_id)
    unit_label = _unit_label(workload_type)
    hardware = workload.get("hardware") or a.get("hardware_sku") or "—"
    hardware_sku = a.get("hardware_sku") or workload.get("hardware_sku")

    ts = rm.get("started_at_utc", "")
    duration_s = float(rm.get("duration_s", 0) or 0)
    samples = int(rm.get("samples", 0) or 0)
    gpu_measured = "no_gpu" not in (summary.get("measured_sources", {}).get("gpu") or [])
    ms = summary.get("measured_sources", {})
    gpu_samples = max(0, samples - int(ms.get("gpu_missing_samples", 0) or 0))
    gpu_coverage_pct = round(100.0 * gpu_samples / samples, 0) if samples else None
    return {
        "run_dir": str(run_dir),
        "run_id": run_id,
        "timestamp": ts,
        "timestamp_display": ts[:16].replace("T", " ") if ts else "—",
        "timestamp_sort": _parse_ts(ts),
        "workload": workload_name,
        "workload_type": workload_type,
        "region": a.get("region", ""),
        "region_label": _region_display(summary, workload),
        "hardware": hardware,
        "hardware_sku": hardware_sku or hardware,
        "facility_wh": round(facility_wh, 1),
        "carbon_g": round(carbon_g, 1),
        "water_ml": round(water_ml, 0),
        "wwl_ml": round(wwl_ml, 1),
        "cost_usd": cost_usd,
        "units": image_count,
        "unit_label": unit_label,
        "per_unit_wh": round(facility_wh / units, 2) if image_count else None,
        "per_unit_carbon_g": round(carbon_g / units, 2) if image_count else None,
        "per_unit_water_ml": round(water_ml / units, 1) if image_count else None,
        "per_unit_wwl_ml": round(wwl_ml / units, 2) if image_count else None,
        "per_unit_cost_usd": round(cost_usd / units, 3) if cost_usd and image_count else None,
        "duration_s": duration_s,
        "samples": samples,
        "gpu_coverage_pct": gpu_coverage_pct,
        "gpu_measured": gpu_measured,
        "cpu_measured": summary.get("measured_sources", {}).get("cpu_rapl_samples", 0) > 0,
        "is_measurement_run": is_measurement_run(gpu_measured, facility_wh, duration_s),
        "notes": (workload.get("notes") or "").strip() or None,
    }


def sort_portfolio_runs(runs: list[dict], sort_by: str = "timestamp") -> list[dict]:
    if sort_by == "timestamp":
        return sorted(runs, key=lambda r: r["timestamp_sort"], reverse=True)
    if sort_by == "region":
        return sorted(runs, key=lambda r: (r["region"], -r["timestamp_sort"]))
    if sort_by == "workload":
        return sorted(runs, key=lambda r: (r["workload"].lower(), -r["timestamp_sort"]))
    if sort_by == "carbon":
        return sorted(runs, key=lambda r: r["carbon_g"], reverse=True)
    if sort_by == "water":
        return sorted(runs, key=lambda r: r["water_ml"], reverse=True)
    if sort_by == "wwl":
        return sorted(runs, key=lambda r: r.get("wwl_ml", r["water_ml"]), reverse=True)
    if sort_by == "cost":
        return sorted(
            runs,
            key=lambda r: (r["cost_usd"] is not None, r["cost_usd"] or 0),
            reverse=True,
        )
    if sort_by == "energy":
        return sorted(runs, key=lambda r: r["facility_wh"], reverse=True)
    raise ValueError(f"unknown sort_by: {sort_by}")


def aggregate_portfolio_summary(runs: list[dict]) -> dict[str, Any]:
    if not runs:
        return {
            "run_count": 0,
            "total_facility_wh": 0,
            "total_carbon_g": 0,
            "total_water_ml": 0,
            "total_wwl_ml": 0,
            "total_cost_usd": None,
            "date_range": None,
            "regions": [],
            "workloads": [],
            "hardware_skus": [],
        }

    timestamps = [r["timestamp_sort"] for r in runs if r["timestamp_sort"]]
    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    regions = sorted({r["region"] for r in runs if r["region"]})
    workloads = sorted({r["workload"] for r in runs if r["workload"]})
    skus = sorted({r["hardware_sku"] for r in runs if r["hardware_sku"] and r["hardware_sku"] != "—"})

    date_range = None
    if timestamps:
        from datetime import datetime, timezone
        earliest = datetime.fromtimestamp(min(timestamps), tz=timezone.utc).strftime("%Y-%m-%d")
        latest = datetime.fromtimestamp(max(timestamps), tz=timezone.utc).strftime("%Y-%m-%d")
        date_range = {"earliest": earliest, "latest": latest}

    return {
        "run_count": len(runs),
        "total_facility_wh": round(sum(r["facility_wh"] for r in runs), 1),
        "total_wwl_ml": round(sum(r.get("wwl_ml", r["water_ml"]) for r in runs), 0),
        "total_carbon_g": round(sum(r["carbon_g"] for r in runs), 1),
        "total_water_ml": round(sum(r["water_ml"] for r in runs), 0),
        "total_cost_usd": round(sum(costs), 2) if costs else None,
        "date_range": date_range,
        "regions": regions,
        "workloads": workloads,
        "hardware_skus": skus,
    }


def build_regional_chart(runs: list[dict]) -> list[dict]:
    by_region: dict[str, dict] = {}
    for run in runs:
        region = run["region"]
        if not region:
            continue
        if region not in by_region:
            by_region[region] = {
                "region": region,
                "label": run["region_label"].split("·")[0].strip(),
                "carbon_g": 0.0,
                "water_ml": 0.0,
                "run_count": 0,
            }
        by_region[region]["carbon_g"] += run["carbon_g"]
        by_region[region]["water_ml"] += run["water_ml"]
        by_region[region]["run_count"] += 1
    rows = list(by_region.values())
    for row in rows:
        row["carbon_g"] = round(row["carbon_g"], 1)
        row["water_ml"] = round(row["water_ml"], 0)
    return sorted(rows, key=lambda r: r["carbon_g"], reverse=True)


def build_regional_per_unit_chart(measurement_runs: list[dict]) -> list[dict]:
    """Per-unit footprint aggregated by region (weighted by units)."""
    by_region: dict[str, dict] = {}
    for run in measurement_runs:
        region = run.get("region")
        units = run.get("units")
        if not region or not units or run.get("per_unit_carbon_g") is None:
            continue
        if region not in by_region:
            by_region[region] = {
                "region": region,
                "label": _region_short(run["region_label"]),
                "unit_label": run.get("unit_label", "unit"),
                "carbon_g": 0.0,
                "facility_wh": 0.0,
                "water_ml": 0.0,
                "units": 0,
                "run_count": 0,
            }
        row = by_region[region]
        row["carbon_g"] += run["carbon_g"]
        row["facility_wh"] += run["facility_wh"]
        row["water_ml"] += run["water_ml"]
        row["units"] += units
        row["run_count"] += 1
    result = []
    for row in by_region.values():
        units = row["units"]
        if not units:
            continue
        result.append({
            "region": row["region"],
            "label": row["label"],
            "unit_label": row["unit_label"],
            "run_count": row["run_count"],
            "per_unit_carbon_g": round(row["carbon_g"] / units, 3),
            "per_unit_wh": round(row["facility_wh"] / units, 3),
            "per_unit_water_ml": round(row["water_ml"] / units, 2),
        })
    return sorted(result, key=lambda r: r["per_unit_carbon_g"], reverse=True)


def compare_pair_key(run_id_a: str, run_id_b: str) -> str:
    return "|".join(sorted([run_id_a, run_id_b]))


def _attach_insight_metadata(insights: list[dict], compare_links: dict[str, str]) -> None:
    for ins in insights:
        low = ins.get("low_run_id")
        high = ins.get("high_run_id")
        if low and high:
            ins["filter_run_ids"] = [low, high]
            ins["compare_link"] = compare_links.get(compare_pair_key(low, high))
        elif ins.get("run_id"):
            ins["filter_run_ids"] = [ins["run_id"]]


def generate_portfolio_compares(
    measurement_runs: list[dict],
    output_path: Path,
    insights: list[dict],
) -> dict[str, str]:
    """Write side-by-side compare dashboards for comparable run pairs."""
    from dashboard import generate_dashboard, load_compare_run, load_run_dir

    by_id = {r["run_id"]: r for r in measurement_runs}
    compare_root = output_path.parent / "portfolio_compare"
    compare_root.mkdir(parents=True, exist_ok=True)
    links: dict[str, str] = {}

    pairs: set[tuple[str, str]] = set()
    for ins in insights:
        low, high = ins.get("low_run_id"), ins.get("high_run_id")
        if low and high and low in by_id and high in by_id:
            pairs.add(tuple(sorted([low, high])))

    comparable = [r for r in measurement_runs if r.get("per_unit_carbon_g") is not None]
    for i, run_a in enumerate(comparable):
        for run_b in comparable[i + 1:]:
            if run_a["workload_type"] != run_b["workload_type"]:
                continue
            if run_a.get("units") != run_b.get("units"):
                continue
            pairs.add(tuple(sorted([run_a["run_id"], run_b["run_id"]])))

    for id_a, id_b in pairs:
        key = compare_pair_key(id_a, id_b)
        if key in links:
            continue
        primary = by_id[id_a]
        secondary = by_id[id_b]
        pair_name = f"{primary['run_id']}_vs_{secondary['run_id']}"
        pair_dir = compare_root / pair_name
        pair_dir.mkdir(parents=True, exist_ok=True)
        try:
            p_summary, p_samples, p_workload = load_run_dir(Path(primary["run_dir"]))
            c_summary, c_samples, c_workload = load_compare_run(Path(secondary["run_dir"]))
            generate_dashboard(
                p_summary,
                p_samples,
                pair_dir,
                workload=p_workload,
                compare_summary=c_summary,
                compare_samples=c_samples,
                compare_workload=c_workload,
            )
            rel = os.path.relpath(pair_dir / "dashboard.html", output_path.parent)
            links[key] = rel.replace("\\", "/")
        except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError, KeyError, StopIteration):
            continue
    return links


def build_workload_chart(runs: list[dict]) -> list[dict]:
    by_workload: dict[str, dict] = {}
    for run in runs:
        key = run["workload"]
        if key not in by_workload:
            by_workload[key] = {
                "workload": key,
                "workload_type": run["workload_type"],
                "unit_label": run["unit_label"],
                "run_count": 0,
                "total_units": 0,
                "facility_wh": 0.0,
                "carbon_g": 0.0,
                "water_ml": 0.0,
            }
        row = by_workload[key]
        row["run_count"] += 1
        row["facility_wh"] += run["facility_wh"]
        row["carbon_g"] += run["carbon_g"]
        row["water_ml"] += run["water_ml"]
        if run["units"]:
            row["total_units"] += run["units"]
    result = []
    for row in by_workload.values():
        units = row["total_units"] or None
        if not units:
            continue
        result.append({
            **row,
            "facility_wh": round(row["facility_wh"], 1),
            "carbon_g": round(row["carbon_g"], 1),
            "water_ml": round(row["water_ml"], 0),
            "per_unit_wh": round(row["facility_wh"] / units, 2),
            "per_unit_carbon_g": round(row["carbon_g"] / units, 2),
            "per_unit_water_ml": round(row["water_ml"] / units, 1),
        })
    return sorted(result, key=lambda r: r["per_unit_carbon_g"], reverse=True)


def build_trend_series(runs: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for run in runs:
        key = f"{run['workload']}|{run['region']}"
        groups.setdefault(key, []).append(run)
    series = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        workload, region = key.split("|", 1)
        points = sorted(group, key=lambda r: r["timestamp_sort"])
        series.append({
            "workload": workload,
            "region": region,
            "region_label": points[0]["region_label"],
            "points": [{
                "timestamp": p["timestamp_display"],
                "timestamp_sort": p["timestamp_sort"],
                "facility_wh": p["facility_wh"],
                "carbon_g": p["carbon_g"],
                "water_ml": p["water_ml"],
                "run_id": p["run_id"],
            } for p in points],
        })
    return series


def _region_short(label: str) -> str:
    return label.split("·")[0].strip() if label else label


def build_insights(measurement_runs: list[dict]) -> list[dict[str, Any]]:
    """Auto-generated insight bullets from measurement runs."""
    if not measurement_runs:
        return [{
            "type": "info",
            "text": "No measurement runs yet. Run a workload with GPU measurement or ≥30s duration.",
        }]

    insights: list[dict[str, Any]] = []
    total_wwl = sum(r.get("wwl_ml", r["water_ml"]) for r in measurement_runs)
    total_carbon = sum(r["carbon_g"] for r in measurement_runs)
    insights.append({
        "type": "summary",
        "text": (
            f"{len(measurement_runs)} measurement run(s) · "
            f"{total_wwl:.0f} mL WWL (watershed-weighted water) · "
            f"{total_carbon:.1f} g CO₂e (carbon context)."
        ),
    })

    groups: dict[tuple[str, int | None], list[dict]] = {}
    for run in measurement_runs:
        if run.get("per_unit_wwl_ml") is None and run.get("per_unit_water_ml") is None:
            continue
        key = (run.get("workload_type", "other"), run.get("units"))
        groups.setdefault(key, []).append(run)

    for group in groups.values():
        if len(group) < 2:
            continue
        wwl_key = "per_unit_wwl_ml" if group[0].get("per_unit_wwl_ml") is not None else "per_unit_water_ml"
        low = min(group, key=lambda r: r.get(wwl_key) or 0)
        high = max(group, key=lambda r: r.get(wwl_key) or 0)
        if (high.get(wwl_key) or 0) <= 0:
            continue
        ratio = (high.get(wwl_key) or 0) / max(low.get(wwl_key) or 0.001, 0.001)
        if ratio <= 1.05:
            continue
        unit = low.get("unit_label") or "unit"
        low_carbon = min(group, key=lambda r: r.get("per_unit_carbon_g") or 0)
        high_carbon = max(group, key=lambda r: r.get("per_unit_carbon_g") or 0)
        carbon_ratio = (
            (high_carbon.get("per_unit_carbon_g") or 0) / max(low_carbon.get("per_unit_carbon_g") or 0.001, 0.001)
            if low_carbon.get("per_unit_carbon_g") else None
        )
        carbon_note = (
            f" ({carbon_ratio:.1f}× carbon spread across same workload)"
            if carbon_ratio and carbon_ratio > 1.05 else ""
        )
        insights.append({
            "type": "regional",
            "text": (
                f"{_region_short(high['region_label'])} uses {ratio:.1f}× more WWL per {unit} "
                f"than {_region_short(low['region_label'])} "
                f"({high.get(wwl_key):.2f} vs {low.get(wwl_key):.2f} mL){carbon_note}."
            ),
            "low_run_id": low["run_id"],
            "high_run_id": high["run_id"],
        })

    cost_runs = [r for r in measurement_runs if r.get("cost_usd") and r.get("per_unit_carbon_g")]
    if len(cost_runs) >= 2:
        best_value = min(
            cost_runs,
            key=lambda r: (r["per_unit_carbon_g"] or 0) + (r["per_unit_cost_usd"] or 0) * 0.01,
        )
        insights.append({
            "type": "recommendation",
            "text": (
                f"Lowest combined carbon+cost per {best_value.get('unit_label', 'unit')}: "
                f"{_region_short(best_value['region_label'])} "
                f"({best_value['per_unit_carbon_g']:.2f} g, "
                f"${best_value['per_unit_cost_usd']:.3f}/unit)."
            ),
            "run_id": best_value["run_id"],
        })

    if len(measurement_runs) >= 2:
        by_carbon = sorted(measurement_runs, key=lambda r: r["carbon_g"])
        lowest = by_carbon[0]
        highest = by_carbon[-1]
        if highest["carbon_g"] > lowest["carbon_g"] * 1.05:
            insights.append({
                "type": "delta",
                "text": (
                    f"Total run carbon ranges from {lowest['carbon_g']:.1f} g "
                    f"({lowest['run_id']}) to {highest['carbon_g']:.1f} g ({highest['run_id']})."
                ),
            })

    return insights[:6]


def build_executive_summary(
    summary: dict[str, Any],
    insights: list[dict[str, Any]],
) -> list[str]:
    """Three–four bullets for board/exec view."""
    bullets: list[str] = []
    if summary.get("run_count"):
        cost = summary.get("total_cost_usd")
        cost_part = f" · ${cost:.2f} compute" if cost is not None else ""
        bullets.append(
            f"{summary['run_count']} measurement runs · "
            f"{summary['total_carbon_g']:.1f} g CO₂e · "
            f"{summary['total_facility_wh']:.1f} Wh{cost_part}"
        )
    for insight in insights:
        if insight["type"] in ("regional", "recommendation") and len(bullets) < 3:
            bullets.append(insight["text"])
    if summary.get("date_range") and len(bullets) < 4:
        dr = summary["date_range"]
        bullets.append(f"Reporting period: {dr['earliest']} → {dr['latest']}")
    return bullets[:4]


def build_water_carbon_scatter(measurement_runs: list[dict]) -> list[dict[str, Any]]:
    """Per-run carbon vs watershed-weighted water for tradeoff scatter."""
    rows = []
    for run in measurement_runs:
        rows.append({
            "run_id": run["run_id"],
            "label": run["run_id"],
            "region": run["region"],
            "region_label": _region_short(run["region_label"]),
            "carbon_g": run["carbon_g"],
            "wwl_ml": run.get("wwl_ml", run["water_ml"]),
            "water_ml": run["water_ml"],
            "per_unit_wwl_ml": run.get("per_unit_wwl_ml"),
            "per_unit_carbon_g": run.get("per_unit_carbon_g"),
        })
    return rows


def build_cost_carbon_chart(measurement_runs: list[dict]) -> list[dict[str, Any]]:
    rows = []
    for run in measurement_runs:
        if run.get("cost_usd") is None:
            continue
        rows.append({
            "run_id": run["run_id"],
            "label": run["run_id"],
            "region": run["region"],
            "region_label": _region_short(run["region_label"]),
            "cost_usd": run["cost_usd"],
            "carbon_g": run["carbon_g"],
            "per_unit_carbon_g": run.get("per_unit_carbon_g"),
        })
    return rows


def _portfolio_fingerprint(runs: list[dict]) -> str:
    canonical = json.dumps(
        [{k: r[k] for k in sorted(r) if k != "dashboard_link"} for r in runs],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_attestation(
    runs: list[dict],
    *,
    scan_dir: str | None,
    generated_at_utc: str | None,
    measurement_count: int,
) -> dict[str, Any]:
    return {
        "scan_dir": scan_dir,
        "runs_scanned": len(runs),
        "measurement_runs": measurement_count,
        "data_fingerprint": _portfolio_fingerprint(runs),
        "generated_at_utc": generated_at_utc,
        "methodology_version": METHODOLOGY_VERSION,
    }


def load_previous_snapshot(snapshot_path: Path) -> dict[str, Any] | None:
    if not snapshot_path.is_file():
        return None
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def compute_snapshot_diff(
    previous: dict[str, Any] | None,
    current_summary: dict[str, Any],
    measurement_run_ids: list[str],
    fingerprint: str,
) -> dict[str, Any] | None:
    if not previous:
        return None
    prev_ids = set(previous.get("measurement_run_ids") or [])
    curr_ids = set(measurement_run_ids)
    new_ids = sorted(curr_ids - prev_ids)
    removed_ids = sorted(prev_ids - curr_ids)
    prev_summary = previous.get("summary") or {}
    carbon_delta = None
    if prev_summary.get("total_carbon_g") is not None and current_summary.get("total_carbon_g") is not None:
        carbon_delta = round(
            current_summary["total_carbon_g"] - prev_summary["total_carbon_g"], 1,
        )
    return {
        "previous_generated_at_utc": previous.get("generated_at_utc"),
        "previous_fingerprint": previous.get("data_fingerprint"),
        "new_measurement_run_ids": new_ids,
        "removed_measurement_run_ids": removed_ids,
        "carbon_delta_g": carbon_delta,
        "fingerprint_changed": previous.get("data_fingerprint") != fingerprint,
    }


def write_portfolio_snapshot(
    snapshot_path: Path,
    *,
    generated_at_utc: str,
    summary: dict[str, Any],
    measurement_run_ids: list[str],
    fingerprint: str,
) -> None:
    snapshot_path.write_text(json.dumps({
        "generated_at_utc": generated_at_utc,
        "summary": summary,
        "measurement_run_ids": measurement_run_ids,
        "data_fingerprint": fingerprint,
    }, indent=2), encoding="utf-8")


def build_portfolio_payload(
    runs: list[dict],
    *,
    sort_by: str = "timestamp",
    scan_dir: str | None = None,
    generated_at_utc: str | None = None,
    methodology_href: str | None = None,
) -> dict[str, Any]:
    sorted_runs = sort_portfolio_runs(runs, sort_by)
    measurement_runs = [r for r in sorted_runs if r.get("is_measurement_run")]
    summary = aggregate_portfolio_summary(measurement_runs)
    insights = build_insights(measurement_runs)
    fingerprint = _portfolio_fingerprint(sorted_runs)
    attestation = build_attestation(
        sorted_runs,
        scan_dir=scan_dir,
        generated_at_utc=generated_at_utc,
        measurement_count=len(measurement_runs),
    )
    return {
        "schema_version": "0.1-portfolio",
        "scan_dir": scan_dir,
        "sort_by": sort_by,
        "meta": {
            "generated_at_utc": generated_at_utc,
            "methodology_version": METHODOLOGY_VERSION,
            "methodology_href": methodology_href,
            "total_runs_scanned": len(sorted_runs),
            "portfolio_href": None,
        },
        "summary": summary,
        "executive_summary": build_executive_summary(summary, insights),
        "insights": insights,
        "attestation": attestation,
        "snapshot_diff": None,
        "runs": sorted_runs,
        "regional_chart": build_regional_chart(measurement_runs),
        "regional_per_unit_chart": build_regional_per_unit_chart(measurement_runs),
        "workload_chart": build_workload_chart(measurement_runs),
        "cost_carbon_chart": build_cost_carbon_chart(measurement_runs),
        "water_carbon_scatter": build_water_carbon_scatter(measurement_runs),
        "trends": build_trend_series(measurement_runs),
        "compare_links": {},
    }


def discover_and_load_runs(root: Path) -> list[dict]:
    run_dirs = discover_run_dirs(root)
    runs = []
    for run_dir in run_dirs:
        try:
            runs.append(load_portfolio_run(run_dir))
        except (ValueError, json.JSONDecodeError, KeyError, OSError):
            continue
    return runs


def generate_portfolio(
    scan_dir: Path,
    output_path: Path,
    *,
    sort_by: str = "timestamp",
    template_path: Path | None = None,
) -> Path:
    """Scan directory for runs and write portfolio HTML."""
    template = template_path or PORTFOLIO_TEMPLATE
    if not template.is_file():
        raise FileNotFoundError(f"portfolio template not found: {template}")

    scan_dir = scan_dir.resolve()
    output_path = output_path.resolve()
    if output_path.suffix != ".html":
        output_path = output_path / "portfolio.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    runs = discover_and_load_runs(scan_dir)
    for run in runs:
        run["dashboard_link"] = _relative_dashboard_link(
            output_path, Path(run["run_dir"]),
        )

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meth_path = output_path.parent / "METHODOLOGY.md"
    methodology_href = (
        os.path.relpath(meth_path, output_path.parent)
        if meth_path.is_file() else "./METHODOLOGY.md"
    )

    payload = build_portfolio_payload(
        runs,
        sort_by=sort_by,
        scan_dir=str(scan_dir),
        generated_at_utc=generated_at,
        methodology_href=methodology_href,
    )
    payload["meta"]["portfolio_href"] = output_path.name
    payload["meta"]["regenerate_command"] = (
        f"watermark --portfolio-dir {scan_dir} --output {output_path.name}"
    )
    measurement_ids = [r["run_id"] for r in payload["runs"] if r.get("is_measurement_run")]
    snapshot_path = output_path.with_name(output_path.stem + ".snapshot.json")
    previous = load_previous_snapshot(snapshot_path)
    payload["snapshot_diff"] = compute_snapshot_diff(
        previous,
        payload["summary"],
        measurement_ids,
        payload["attestation"]["data_fingerprint"],
    )
    write_portfolio_snapshot(
        snapshot_path,
        generated_at_utc=generated_at,
        summary=payload["summary"],
        measurement_run_ids=measurement_ids,
        fingerprint=payload["attestation"]["data_fingerprint"],
    )
    measurement_runs = [r for r in payload["runs"] if r.get("is_measurement_run")]
    compare_links = generate_portfolio_compares(
        measurement_runs, output_path, payload["insights"],
    )
    payload["compare_links"] = compare_links
    _attach_insight_metadata(payload["insights"], compare_links)
    html = template.read_text(encoding="utf-8")
    if PORTFOLIO_DATA_MARKER not in html:
        raise ValueError(f"portfolio template missing marker {PORTFOLIO_DATA_MARKER}")

    inject = f"window.WATERMARK_PORTFOLIO_DATA = {json.dumps(payload, indent=2)};"
    html = html.replace(PORTFOLIO_DATA_MARKER, inject, 1)
    html = html.replace(
        "<title>Watermark — Portfolio</title>",
        f"<title>Watermark — Portfolio · {payload['summary']['run_count']} runs</title>",
        1,
    )
    output_path.write_text(html, encoding="utf-8")
    return output_path


def portfolio_cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate a portfolio dashboard from many Watermark runs.",
    )
    p.add_argument("scan_dir", type=Path,
                   help="Root directory to scan for summary.json files.")
    p.add_argument("--output", type=Path, default=Path("portfolio.html"),
                   help="Output HTML file (default: ./portfolio.html).")
    p.add_argument("--sort-by", choices=SORT_CHOICES, default="timestamp",
                   help="Default sort order for runs table (default: timestamp).")
    p.add_argument("--template", type=Path, default=None,
                   help="Portfolio HTML template (default: portfolio_template.html).")
    args = p.parse_args(argv)

    path = generate_portfolio(
        args.scan_dir, args.output,
        sort_by=args.sort_by,
        template_path=args.template,
    )
    count = len(discover_run_dirs(args.scan_dir))
    print(f"[watermark] portfolio: found {count} run(s)")
    print(f"[watermark] wrote {path}")
    return 0


def resolve_portfolio_output(output: str | Path) -> Path:
    out = Path(output)
    if out.suffix == ".html":
        return out
    return out / "portfolio.html"


def resolve_portfolio_scan_dir(
    run_output_dir: Path,
    *,
    portfolio_dir: Path | None = None,
) -> Path | None:
    """
    Portfolio scan root from --portfolio-dir, WATERMARK_PORTFOLIO_DIR, or run layout.

    When a run folder lives inside a multi-run workspace (e.g. ~/watermark/in_run_v01),
    the parent directory is used automatically if it already contains runs or a portfolio.
    """
    if portfolio_dir is not None:
        return portfolio_dir.resolve()
    env = os.environ.get("WATERMARK_PORTFOLIO_DIR")
    if env:
        return Path(env).expanduser().resolve()
    run_dir = run_output_dir.resolve()
    parent = run_dir.parent
    if (parent / "portfolio.html").is_file() or (parent / "portfolio.snapshot.json").is_file():
        return parent
    if discover_run_dirs(parent):
        return parent
    return None


def regenerate_portfolio_after_run(
    run_output_dir: Path,
    *,
    portfolio_dir: Path | None = None,
    portfolio_output: Path | None = None,
    sort_by: str = "timestamp",
) -> Path | None:
    """Refresh portfolio.html after a measurement run when a scan dir is configured."""
    scan_dir = resolve_portfolio_scan_dir(run_output_dir, portfolio_dir=portfolio_dir)
    if scan_dir is None:
        return None
    out = portfolio_output or (scan_dir / "portfolio.html")
    return generate_portfolio(scan_dir, out, sort_by=sort_by)
