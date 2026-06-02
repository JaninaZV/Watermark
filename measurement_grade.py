"""Measurement confidence grades for audit-ready summary.json output."""

from __future__ import annotations

from typing import Any

MEASUREMENT_GRADES = frozenset({"A", "B", "C"})
GRADE_LIMITING_FACTORS = frozenset({
    "cooling_type_unknown",
    "rapl_interrupted",
    "cpu_modeled",
    "gpu_not_measured",
    "default_pue",
    "default_wue",
    "water_accounting_unknown",
    "region_unknown",
    "host_measurement_unavailable",
})


def _gpu_measured(summary: dict[str, Any]) -> bool:
    measured = summary.get("measured_sources", {})
    gpu_sources = measured.get("gpu") or []
    if gpu_sources == ["no_gpu"]:
        return True
    return int(measured.get("gpu_missing_samples", 0) or 0) == 0


def compute_measurement_grade(
    summary: dict[str, Any],
    *,
    rapl_available_at_start: bool,
) -> tuple[str, str | None]:
    """
    Roll up measurement confidence. Grade reflects worst condition during the run.

    A — RAPL/NVML measured, known region, known cooling, declared water accounting
    B — modeled CPU/GPU, known region, no grade-C limiting factors
    C — VM/host unknown, default PUE/WUE, cooling unknown, or RAPL interrupted
    """
    assumptions = summary.get("assumptions", {})
    measured = summary.get("measured_sources", {})
    water = summary.get("water", {})

    region = assumptions.get("region", "")
    cooling = assumptions.get("cooling_type", "unknown")
    pue_source = assumptions.get("pue_source", "")
    wue_source = assumptions.get("wue_source", "")
    accounting = water.get("water_accounting_method", "unknown")

    cpu_rapl = int(measured.get("cpu_rapl_samples", 0) or 0)
    cpu_modeled = int(measured.get("cpu_modeled_samples", 0) or 0)
    samples = int(summary.get("run_metadata", {}).get("samples", 0) or 0)

    c_factors: list[str] = []

    if region in ("", "global-avg"):
        c_factors.append("region_unknown")
    if cooling == "unknown":
        c_factors.append("cooling_type_unknown")
    if pue_source == "default_iea_2024":
        c_factors.append("default_pue")
    if wue_source != "user_override":
        c_factors.append("default_wue")
    if rapl_available_at_start and cpu_modeled > 0:
        c_factors.append("rapl_interrupted")
    if cpu_rapl == 0 and samples > 0:
        c_factors.append("host_measurement_unavailable")

    if c_factors:
        return "C", c_factors[0]

    cpu_all_rapl = cpu_modeled == 0 and cpu_rapl > 0
    gpu_ok = _gpu_measured(summary)

    if cpu_all_rapl and gpu_ok and accounting in ("consumption", "withdrawal"):
        return "A", None

    b_factors: list[str] = []
    if not cpu_all_rapl:
        b_factors.append("cpu_modeled")
    if not gpu_ok:
        b_factors.append("gpu_not_measured")
    if accounting == "unknown":
        b_factors.append("water_accounting_unknown")

    if b_factors:
        return "B", b_factors[0]

    return "A", None


def grade_caveat(grade: str, limiting_factor: str | None) -> dict[str, str] | None:
    from schema_contract import make_caveat

    if grade == "A":
        return None
    label = limiting_factor or "unknown"
    severity = "warning" if grade == "B" else "error"
    return make_caveat(
        "measurement_grade_limited",
        severity,
        f"Measurement grade {grade} — limiting factor: {label}. "
        "See SCHEMA.md tier definitions; improve inputs (RAPL host, --pue, --cooling-system, "
        "--water-source operator) for higher confidence.",
    )
