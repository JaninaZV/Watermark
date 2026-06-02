"""Workload-reported unit counts for per-token/request normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKLOAD_METRICS_FILENAME = "workload_metrics.json"
PRE_ANNOTATION_SUMMARY_FILENAME = "summary.pre_annotation.json"

# Priority when multiple counts are present (training vs inference semantics differ).
_UNIT_PRIORITY: tuple[tuple[str, str], ...] = (
    ("training_step", "training_steps"),
    ("token", "token_count"),
    ("request", "request_count"),
    ("image", "image_count"),
)

_TOKEN_ALIASES = ("token_count", "completion_tokens", "total_tokens")


def write_workload_metrics(output_dir: Path, metrics: dict[str, Any]) -> Path:
    """Write metrics from a workload script (read by watermark after the command exits)."""
    path = Path(output_dir) / WORKLOAD_METRICS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in metrics.items() if v is not None}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_workload_metrics(run_dir: Path) -> dict[str, Any] | None:
    """Load workload_metrics.json if the wrapped workload wrote it."""
    path = Path(run_dir) / WORKLOAD_METRICS_FILENAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid {WORKLOAD_METRICS_FILENAME}: expected object")
    return data


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def normalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Normalize field names and aliases from workload or workload.json."""
    out = dict(metrics)
    if out.get("token_count") is None:
        for key in _TOKEN_ALIASES:
            if key != "token_count" and out.get(key) is not None:
                out["token_count"] = out[key]
                break
    if out.get("request_count") is None and out.get("completion_count") is not None:
        out["request_count"] = out["completion_count"]
    return out


def resolve_unit_normalization(
    metrics: dict[str, Any] | None,
    *,
    token_count: int | None = None,
    request_count: int | None = None,
    training_steps: int | None = None,
    image_count: int | None = None,
) -> tuple[str | None, int | None, str | None]:
    """
    Pick unit type/count for per_unit block.

    Explicit CLI counts win over workload-reported metrics.
    Returns (unit_type, unit_count, source_tag).
    """
    cli = {
        "training_steps": _coerce_positive_int(training_steps),
        "token_count": _coerce_positive_int(token_count),
        "request_count": _coerce_positive_int(request_count),
        "image_count": _coerce_positive_int(image_count),
    }
    for _unit_type, field in _UNIT_PRIORITY:
        if cli.get(field):
            return _unit_type, cli[field], "cli"

    if not metrics:
        return None, None, None

    merged = normalize_metrics(metrics)
    for unit_type, field in _UNIT_PRIORITY:
        count = _coerce_positive_int(merged.get(field))
        if count:
            source = merged.get("source") or WORKLOAD_METRICS_FILENAME
            return unit_type, count, str(source)

    return None, None, None


def build_per_unit_from_summary(
    summary: dict[str, Any],
    unit_type: str,
    unit_count: int,
    *,
    normalization_source: str,
) -> dict[str, Any]:
    water = summary["water"]
    energy = summary["energy"]
    carbon = summary["carbon"]
    facility_wh = energy["facility_total_kwh"] * 1000
    wwl_ml = water.get("wwl_ml", water.get("stress_weighted_total_l", water["total_l"]) * 1000)
    carbon_g = carbon["co2e_kg"] * 1000
    return {
        "unit_type": unit_type,
        "unit_count": unit_count,
        "wwl_ml_per_unit": round(wwl_ml / unit_count, 6),
        "energy_wh_per_unit": round(facility_wh / unit_count, 6),
        "carbon_g_per_unit": round(carbon_g / unit_count, 6),
        "normalization_source": normalization_source,
    }


def preserve_pre_annotation_summary(run_dir: Path, summary: dict[str, Any]) -> Path | None:
    """
    Snapshot summary.json before post-hoc annotation (written once, never overwritten).

    The pre-annotation file is the measurement-time artifact for audit totals;
    summary.json after annotate is a derived artifact with per_unit normalization.
    """
    path = Path(run_dir) / PRE_ANNOTATION_SUMMARY_FILENAME
    if path.is_file():
        return None
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def is_post_measurement_normalization(summary: dict[str, Any]) -> bool:
    """True when per_unit was applied after the original measurement artifact."""
    if summary.get("normalization_applied_post_measurement") is True:
        return True
    per_unit = summary.get("per_unit") or {}
    source = str(per_unit.get("normalization_source", ""))
    if source == "watermark annotate":
        return True
    caveats = summary.get("caveats") or []
    return any(c.get("code") == "per_unit_post_hoc" for c in caveats if isinstance(c, dict))


def apply_per_unit_to_summary(
    summary: dict[str, Any],
    unit_type: str,
    unit_count: int,
    *,
    normalization_source: str,
    post_hoc: bool = False,
) -> dict[str, Any]:
    """Attach or replace per_unit normalization on an existing summary.json payload."""
    from schema_contract import make_caveat

    per_unit = build_per_unit_from_summary(
        summary,
        unit_type,
        unit_count,
        normalization_source=normalization_source,
    )
    per_unit["applied_post_measurement"] = post_hoc
    summary = dict(summary)
    summary["per_unit"] = per_unit
    summary["normalization_applied_post_measurement"] = post_hoc
    summary["water"] = dict(summary["water"])
    summary["water"]["wwl_per_unit_ml"] = per_unit["wwl_ml_per_unit"]

    rm = dict(summary.get("run_metadata", {}))
    if rm.get("workload_type") in (None, "unknown") and unit_type == "token":
        rm["workload_type"] = "inference"
    elif rm.get("workload_type") in (None, "unknown") and unit_type == "training_step":
        rm["workload_type"] = "training"
    summary["run_metadata"] = rm

    code = "per_unit_post_hoc" if post_hoc else "per_unit_from_workload_metrics"
    message = (
        f"per_unit normalized using {unit_count} {unit_type}(s) from {normalization_source} "
        f"({'after measurement' if post_hoc else 'reported by workload'})."
    )
    caveats = list(summary.get("caveats", []))
    caveats = [c for c in caveats if c.get("code") not in {code, "per_unit_post_hoc", "per_unit_from_workload_metrics"}]
    caveats.append(make_caveat(code, "info", message))
    summary["caveats"] = caveats
    return summary


def load_counts_from_workload_json(run_dir: Path) -> dict[str, Any] | None:
    """Read token/request/image counts from workload.json when present."""
    path = Path(run_dir) / "workload.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    fields = ("token_count", "completion_tokens", "total_tokens", "request_count",
              "training_steps", "image_count", "completion_count")
    picked = {k: data[k] for k in fields if k in data and data[k] is not None}
    return picked or None
