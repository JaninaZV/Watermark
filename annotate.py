"""Post-hoc and workload-sourced per-unit normalization for existing runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workload_metrics import (
    PRE_ANNOTATION_SUMMARY_FILENAME,
    apply_per_unit_to_summary,
    load_counts_from_workload_json,
    load_workload_metrics,
    preserve_pre_annotation_summary,
    resolve_unit_normalization,
    write_workload_metrics,
)


def annotate_run_dir(
    run_dir: Path,
    *,
    token_count: int | None = None,
    request_count: int | None = None,
    training_steps: int | None = None,
    image_count: int | None = None,
    from_workload: bool = False,
    from_metrics: bool = False,
    write_metrics: bool = False,
    regenerate_report: bool = True,
) -> dict:
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"missing summary.json in {run_dir}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    metrics = None

    if from_metrics:
        metrics = load_workload_metrics(run_dir)
        if metrics is None:
            raise FileNotFoundError(f"missing workload_metrics.json in {run_dir}")
    elif from_workload:
        metrics = load_counts_from_workload_json(run_dir)
        if metrics is None:
            raise FileNotFoundError(f"no normalization counts in {run_dir}/workload.json")

    unit_type, unit_count, resolved_source = resolve_unit_normalization(
        metrics,
        token_count=token_count,
        request_count=request_count,
        training_steps=training_steps,
        image_count=image_count,
    )
    if not unit_type or not unit_count:
        raise ValueError(
            "no unit count available — pass --token-count, --request-count, "
            "--training-steps, --image-count, or --from-workload / --from-metrics"
        )

    norm_source = "watermark annotate"
    if from_metrics or from_workload:
        norm_source = resolved_source or "workload_metrics.json"

    preserve_pre_annotation_summary(run_dir, summary)
    summary = apply_per_unit_to_summary(
        summary,
        unit_type,
        unit_count,
        normalization_source=norm_source,
        post_hoc=True,
    )
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if write_metrics:
        write_workload_metrics(run_dir, {
            "token_count": token_count,
            "request_count": request_count,
            "training_steps": training_steps,
            "image_count": image_count,
            "source": "watermark annotate",
        })

    workload_path = run_dir / "workload.json"
    if workload_path.is_file():
        workload = json.loads(workload_path.read_text(encoding="utf-8"))
    else:
        workload = {}
    key_map = {
        "token": "token_count",
        "request": "request_count",
        "training_step": "training_steps",
        "image": "image_count",
    }
    field = key_map.get(unit_type)
    if field:
        workload[field] = unit_count
        workload_path.write_text(json.dumps(workload, indent=2) + "\n", encoding="utf-8")

    if regenerate_report:
        import watermark_meter as wm
        (run_dir / "report.md").write_text(wm.Meter._render_markdown(summary), encoding="utf-8")

    return summary


def annotate_cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Add or update per-unit normalization on an existing run using token/request "
            "counts from flags, workload.json, or workload_metrics.json."
        ),
    )
    p.add_argument("run_dir", type=Path, help="Run directory with summary.json")
    p.add_argument("--token-count", type=int, default=None, dest="token_count")
    p.add_argument("--request-count", type=int, default=None, dest="request_count")
    p.add_argument("--training-steps", type=int, default=None, dest="training_steps")
    p.add_argument("--image-count", type=int, default=None, dest="image_count")
    p.add_argument(
        "--from-workload",
        action="store_true",
        help="Use token_count / request_count / image_count from workload.json",
    )
    p.add_argument(
        "--from-metrics",
        action="store_true",
        help="Use counts from workload_metrics.json (written by reference workloads)",
    )
    p.add_argument(
        "--write-metrics",
        action="store_true",
        help="Also write supplied counts to workload_metrics.json",
    )
    p.add_argument(
        "--dashboard",
        action="store_true",
        help="Regenerate dashboard.html after annotating",
    )
    args = p.parse_args(argv)

    try:
        summary = annotate_run_dir(
            args.run_dir,
            token_count=args.token_count,
            request_count=args.request_count,
            training_steps=args.training_steps,
            image_count=args.image_count,
            from_workload=args.from_workload,
            from_metrics=args.from_metrics,
            write_metrics=args.write_metrics,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pu = summary["per_unit"]
    print(
        f"[watermark] annotate: {pu['wwl_ml_per_unit']:.6f} mL WWL/{pu['unit_type']} "
        f"({pu['unit_count']} {pu['unit_type']}s, source: {pu['normalization_source']})"
    )
    print(f"[watermark] wrote {args.run_dir / 'summary.json'}")
    pre = args.run_dir / PRE_ANNOTATION_SUMMARY_FILENAME
    if pre.is_file():
        print(f"[watermark] preserved measurement-time artifact: {pre}")
        print("[watermark] note: summary.json is a derived artifact after annotate; "
              "audit-pack includes both for compliance review.")

    if args.dashboard:
        from dashboard import generate_dashboard, load_run_dir
        s, samples, workload = load_run_dir(args.run_dir)
        path = generate_dashboard(s, samples, args.run_dir, workload=workload)
        print(f"[watermark] wrote {path}")

    return 0
