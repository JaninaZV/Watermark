"""CI gate for per-unit WWL and carbon thresholds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_GATE_CONFIG_NAME = ".watermark-gate.json"


def load_gate_config(project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or Path.cwd()
    path = root / _GATE_CONFIG_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_thresholds(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> tuple[float | None, float | None]:
    max_wwl = args.max_wwl_ml_per_unit
    max_carbon = args.max_carbon_g_per_unit
    if max_wwl is None:
        max_wwl = config.get("max_wwl_ml_per_unit")
    if max_carbon is None:
        max_carbon = config.get("max_carbon_g_per_unit")
    return max_wwl, max_carbon


def _per_unit_values(summary: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    per_unit = summary.get("per_unit") or {}
    wwl = per_unit.get("wwl_ml_per_unit")
    carbon = per_unit.get("carbon_g_per_unit")
    unit_type = per_unit.get("unit_type")
    if wwl is None and summary.get("water", {}).get("wwl_per_unit_ml") is not None:
        wwl = summary["water"]["wwl_per_unit_ml"]
    return wwl, carbon, unit_type


def evaluate_gate(
    summary: dict[str, Any],
    *,
    max_wwl_ml_per_unit: float | None,
    max_carbon_g_per_unit: float | None,
) -> tuple[bool, str]:
    wwl, carbon, unit_type = _per_unit_values(summary)
    grade = summary.get("measurement_grade", "?")
    limiting = summary.get("grade_limiting_factor")

    failures: list[str] = []
    if max_wwl_ml_per_unit is not None:
        if wwl is None:
            failures.append("no per_unit WWL in summary (pass --token-count, --request-count, etc.)")
        elif wwl > max_wwl_ml_per_unit:
            failures.append(f"WWL {wwl:.3f} mL/unit exceeds limit {max_wwl_ml_per_unit}")
    if max_carbon_g_per_unit is not None:
        if carbon is None:
            failures.append("no per_unit carbon in summary")
        elif carbon > max_carbon_g_per_unit:
            failures.append(f"carbon {carbon:.4f} g/unit exceeds limit {max_carbon_g_per_unit}")

    unit_label = unit_type or "unit"
    if failures:
        msg = (
            f"[watermark] FAIL — {'; '.join(failures)} | grade {grade}"
            + (f" | {limiting}" if limiting else "")
        )
        return False, msg

    wwl_disp = f"{wwl:.3f}" if wwl is not None else "n/a"
    carbon_disp = f"{carbon:.4f}" if carbon is not None else "n/a"
    status = "PASS"
    if grade == "C":
        status = "PASS (grade C warning)"
    msg = (
        f"[watermark] {status} — {wwl_disp} mL WWL/{unit_label}"
        + (f" (limit {max_wwl_ml_per_unit})" if max_wwl_ml_per_unit is not None else "")
        + f" | grade {grade}"
        + (f" | {limiting}" if limiting and grade == "C" else "")
    )
    return True, msg


def gate_cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CI gate on per-unit WWL and carbon thresholds.")
    p.add_argument("run_dir", type=Path, nargs="?", default=None, help="Run directory or summary.json path")
    p.add_argument("--max-wwl-ml-per-unit", type=float, default=None, dest="max_wwl_ml_per_unit")
    p.add_argument("--max-carbon-g-per-unit", type=float, default=None, dest="max_carbon_g_per_unit")
    p.add_argument("--config-root", type=Path, default=Path.cwd())
    args = p.parse_args(argv)

    config = load_gate_config(args.config_root)
    max_wwl, max_carbon = resolve_thresholds(args, config)
    if max_wwl is None and max_carbon is None:
        print("error: set thresholds via flags or .watermark-gate.json", file=sys.stderr)
        return 2

    if args.run_dir is None:
        print("error: run_dir required", file=sys.stderr)
        return 2

    summary_path = args.run_dir / "summary.json" if args.run_dir.is_dir() else args.run_dir
    if not summary_path.is_file():
        print(f"error: missing {summary_path}", file=sys.stderr)
        return 2

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    ok, msg = evaluate_gate(
        summary,
        max_wwl_ml_per_unit=max_wwl,
        max_carbon_g_per_unit=max_carbon,
    )
    print(msg)
    if ok and summary.get("measurement_grade") == "C":
        print(
            "[watermark] warning: grade C — uncertain measurement; gate passed but review limiting factor",
            file=sys.stderr,
        )
    return 0 if ok else 1
