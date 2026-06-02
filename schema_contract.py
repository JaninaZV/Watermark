"""Run and portfolio artifact schema validation (see SCHEMA.md)."""

from __future__ import annotations

import sys
from typing import Any

RUN_SCHEMA_VERSION = "0.3"
PORTFOLIO_SCHEMA_VERSION = "0.1-portfolio"
DEFAULT_RUN_SCHEMA_VERSION = "0.1"

MEASUREMENT_GRADES = frozenset({"A", "B", "C"})
WATER_ACCOUNTING_METHODS = frozenset({"consumption", "withdrawal", "unknown"})
WORKLOAD_TYPES = frozenset({"training", "inference", "benchmark", "unknown"})
PER_UNIT_TYPES = frozenset({"token", "image", "request", "training_step"})

WATER_BOUNDARIES = frozenset({"facility", "grid", "region"})
CAVEAT_SEVERITIES = frozenset({"info", "warning", "error"})

RUN_REQUIRED_TOP_LEVEL = (
    "run_metadata",
    "measured_sources",
    "energy",
    "carbon",
    "water",
    "embodied",
    "lifecycle",
    "assumptions",
    "caveats",
)

PORTFOLIO_REQUIRED_TOP_LEVEL = (
    "schema_version",
    "scan_dir",
    "sort_by",
    "meta",
    "summary",
    "executive_summary",
    "insights",
    "attestation",
    "snapshot_diff",
    "runs",
    "regional_chart",
    "regional_per_unit_chart",
    "workload_chart",
    "cost_carbon_chart",
    "trends",
    "compare_links",
)


def resolve_run_schema_version(summary: dict[str, Any]) -> tuple[str, bool]:
    """Return (version, was_missing). Missing schema_version is treated as 0.1."""
    raw = summary.get("schema_version")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_RUN_SCHEMA_VERSION, True
    return str(raw), False


def warn_missing_run_schema_version(context: str) -> None:
    print(
        f"[watermark] warning: summary.json missing schema_version; "
        f"treating as {DEFAULT_RUN_SCHEMA_VERSION} ({context})",
        file=sys.stderr,
    )


def make_caveat(code: str, severity: str, message: str) -> dict[str, str]:
    if severity not in CAVEAT_SEVERITIES:
        raise ValueError(f"invalid caveat severity: {severity!r}")
    code = code.strip()
    if not code:
        raise ValueError("caveat code must be non-empty")
    return {"code": code, "severity": severity, "message": message}


def normalize_caveat(entry: Any) -> dict[str, str]:
    """Accept structured caveat objects; upgrade legacy string caveats for display."""
    if isinstance(entry, str):
        return make_caveat("legacy_string", "info", entry)
    if not isinstance(entry, dict):
        raise ValueError(f"caveat must be object or string, got {type(entry).__name__}")
    missing = [k for k in ("code", "severity", "message") if k not in entry]
    if missing:
        raise ValueError(f"caveat missing required field(s): {', '.join(missing)}")
    severity = entry["severity"]
    if severity not in CAVEAT_SEVERITIES:
        raise ValueError(f"invalid caveat severity: {severity!r}")
    code = str(entry["code"]).strip()
    if not code:
        raise ValueError("caveat code must be non-empty")
    message = entry["message"]
    if not isinstance(message, str) or not message.strip():
        raise ValueError("caveat message must be a non-empty string")
    return {"code": code, "severity": severity, "message": message}


def caveat_message(entry: Any) -> str:
    return normalize_caveat(entry)["message"]


def validate_caveats(
    caveats: Any,
    *,
    path: str = "caveats",
    allow_legacy_strings: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(caveats, list):
        return [f"{path} must be an array"]
    for index, entry in enumerate(caveats):
        if isinstance(entry, str):
            if allow_legacy_strings:
                continue
            errors.append(
                f"{path}[{index}]: bare string caveats are not valid in new output; "
                "use {{code, severity, message}}"
            )
            continue
        try:
            normalize_caveat(entry)
        except ValueError as exc:
            errors.append(f"{path}[{index}]: {exc}")
    return errors


def validate_run_summary(
    summary: dict[str, Any],
    *,
    allow_legacy_caveats: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(summary, dict):
        return ["summary must be a JSON object"]

    for key in RUN_REQUIRED_TOP_LEVEL:
        if key not in summary:
            errors.append(f"missing required field: {key!r}")

    version, _missing = resolve_run_schema_version(summary)
    if version != RUN_SCHEMA_VERSION and summary.get("schema_version") is not None:
        errors.append(
            f"unsupported schema_version {version!r} (validator targets {RUN_SCHEMA_VERSION!r})"
        )

    if "caveats" in summary:
        errors.extend(
            validate_caveats(
                summary["caveats"],
                allow_legacy_strings=allow_legacy_caveats,
            )
        )

    version, _missing = resolve_run_schema_version(summary)
    if version == RUN_SCHEMA_VERSION or summary.get("schema_version") == RUN_SCHEMA_VERSION:
        grade = summary.get("measurement_grade")
        if grade not in MEASUREMENT_GRADES:
            errors.append(f"measurement_grade must be one of {sorted(MEASUREMENT_GRADES)}")
        limiting = summary.get("grade_limiting_factor")
        if grade in ("B", "C") and not limiting:
            errors.append("grade_limiting_factor required when measurement_grade is B or C")
        wt = summary.get("run_metadata", {}).get("workload_type")
        if wt not in WORKLOAD_TYPES:
            errors.append(f"run_metadata.workload_type must be one of {sorted(WORKLOAD_TYPES)}")
        acct = summary.get("water", {}).get("water_accounting_method")
        if acct not in WATER_ACCOUNTING_METHODS:
            errors.append(f"water.water_accounting_method must be one of {sorted(WATER_ACCOUNTING_METHODS)}")
        per_unit = summary.get("per_unit")
        if per_unit is not None:
            if per_unit.get("unit_type") not in PER_UNIT_TYPES:
                errors.append("per_unit.unit_type invalid")
            if not isinstance(per_unit.get("unit_count"), int) or per_unit["unit_count"] <= 0:
                errors.append("per_unit.unit_count must be positive integer")

    return errors


def validate_portfolio_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["portfolio payload must be a JSON object"]

    for key in PORTFOLIO_REQUIRED_TOP_LEVEL:
        if key not in payload:
            errors.append(f"missing required field: {key!r}")

    version = payload.get("schema_version")
    if version != PORTFOLIO_SCHEMA_VERSION:
        errors.append(
            f"unsupported schema_version {version!r} "
            f"(validator targets {PORTFOLIO_SCHEMA_VERSION!r})"
        )

    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        errors.append("meta must be an object")

    runs = payload.get("runs")
    if runs is not None and not isinstance(runs, list):
        errors.append("runs must be an array")

    return errors
