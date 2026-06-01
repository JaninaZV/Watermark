# Watermark Meter

**Measure the true energy, carbon, and water cost of AI workloads — with every assumption auditable.**

> **Status:** v0.1 · methodology-first · public preview

Watermark is an open-source CLI that meters a running workload and produces
reproducible footprint reports: per-sample readings, aggregated totals, and
interactive HTML dashboards. It reads hardware where possible (RAPL, NVML) and
models the rest from published regional constants — tagging every number with
its source so reviewers can challenge or replace any assumption.

**Quick start**

```bash
pip install -e .
watermark --help
```

Full methodology: [`METHODOLOGY.md`](./METHODOLOGY.md) · License: [Apache 2.0](./LICENSE) ·
Contributions: [CONTRIBUTING.md](./CONTRIBUTING.md)

---

## What's in scope for v0.1

- **Energy** — CPU (RAPL or utilization×TDP) + GPU (NVML / nvidia-smi), integrated over time
- **Carbon** — operational CO₂e from regional grid intensity (EPA eGRID + IEA static profiles)
- **Water** — direct cooling (WUE) + indirect generation water (NREL / USGS)
- **Watershed stress** — WRI Aqueduct basin weighting on operational water totals
- **Embodied impact** — optional amortized manufacturing carbon/water via `--hardware-sku`
- **Comparison** — same workload across regions; side-by-side run dashboards and portfolio view
- **Audit trail** — `measurements.csv`, `summary.json`, `report.md`, auto-generated `dashboard.html`

## What's not yet supported

- Per-process attribution (no eBPF / Kepler-style PID scope — planned v0.3)
- Real-time grid carbon as default (ElectricityMaps optional via env var; static table is default)
- Hosted multi-tenant SaaS or always-on observability agent
- Full ISO product LCA for embodied impact (preview amortization only)
- Memory, network, and storage power as separate measured channels

---

Unlike a one-liner that multiplies `cpu_percent` by an invented wattage, this tool:

- reads CPU energy from Intel/AMD RAPL hardware counters when available
- reads GPU power from NVIDIA NVML / nvidia-smi when a GPU is present
- applies a regional PUE for cooling overhead
- applies a regional grid carbon intensity (kgCO₂e per kWh) from EPA eGRID + IEA
- applies both direct cooling water (WUE) and indirect generation water (NREL/USGS)
- applies optional WRI Aqueduct watershed-stress weighting on operational water
- supports optional embodied carbon/water when `--hardware-sku` is set
- tags every output line with whether the underlying number was measured or modeled

## Install

Requires Python 3.9+.

```bash
pip install -e .
watermark --help   # verify
```

Optional extras:

```bash
pip install -e ".[gpu]"    # NVIDIA NVML
pip install -e ".[dev]"    # pytest for tests
pip install -e ".[gpu,dev]"
```

If `pynvml` isn't installed, the tool falls back to shelling out to `nvidia-smi`.

You can also run without installing: `python3 watermark_meter.py --help`

## Pod quickstart

On [RunPod](https://www.runpod.io/), use a **PyTorch / Hugging Face** template pod, then:

```bash
git clone https://github.com/JaninaZV/Watermark.git
cd watermark && bash pod_setup.sh
```

Optionally set live grid carbon (static regional profiles work without this):

```bash
export ELECTRICITYMAPS_API_KEY="your-key"
```

Example measurement:

```bash
watermark --duration 60 --region us-east-1 --output ./run1
```

**Tip:** Mount a network volume at `~/.cache/huggingface/` so large models (14GB+) are not re-downloaded on every pod restart.

## Quickstart

Measure for 60 seconds in US-East (Virginia):

```bash
watermark --duration 60 --region us-east-1 --output ./run1
```

Wrap a command and measure while it runs:

```bash
watermark --region eu-west-1 --output ./run1 -- python train.py
```

Full experiment run (meter + dashboard + workload metadata):

```bash
watermark --region us-east-1 --output ./my_run \
  --run-id sdxl-100-v01 \
  --workload-name "SDXL · 100 imgs" \
  --image-count 100 --hardware "H100 SXM 80GB" \
  --hardware-sku h100-sxm \
  --model "stabilityai/stable-diffusion-xl-base-1.0" \
  --facility-location "US East (cloud provider)" \
  --seed 42 --steps 30 --compute-rate 3.30 \
  -- python3 generate_images.py --count 100 --output ./my_run/images
```

## Output

Each run writes into the `--output` directory:

| File | Role |
|------|------|
| `measurements.csv` | Per-sample readings (raw audit trail) |
| `summary.json` | Aggregated totals + assumptions (**source of truth** for math) |
| `report.md` | Human-readable summary |
| `workload.json` | Experiment metadata (see below) |
| `dashboard.html` | Interactive footprint dashboard (auto when `--run-id` or `--image-count` set) |

**Dashboard defaults:** off for quick dev runs. Auto-generated when you pass `--run-id` or `--image-count`, or force with `--dashboard`. Use `--no-dashboard` to skip.

### Experiment metadata flags

These write into `workload.json` only (not `summary.json`). Omitted flags → key absent (never `null`).

| Flag | Example | Purpose |
|------|---------|---------|
| `--model` | `stabilityai/stable-diffusion-xl-base-1.0` | Model used for the workload |
| `--facility-location` | `Norway (EUR-IS-3)` | Physical datacenter (free text) |
| `--cooling-system` | `evaporative` | Cooling type if known — omit if unknown |
| `--notes` | `"50 imgs, seed 42"` | Free-form run notes |

`--region` selects the **grid profile** for carbon/water math. `--facility-location` describes where the hardware actually ran — they are intentionally separate.

Also available: `--run-id`, `--workload-name`, `--image-count` (unit count for per-item KPIs), `--hardware`, `--hardware-sku`, `--steps`, `--seed`, `--compute-rate`, `--compare-run`.

### Regenerate dashboard without re-measuring

```bash
python -m dashboard ./my_run
```

Override metadata on regen:

```bash
python -m dashboard ./my_run \
  --model "stabilityai/stable-diffusion-xl-base-1.0" \
  --facility-location "US East (cloud provider)"
```

### Per-run dashboard (v0.1)

- KPI cards with per-unit toggle; lifecycle totals when `--hardware-sku` set
- Power time series, regional counterfactual panels, scale context
- Side-by-side run comparison (`--compare-run`)
- Sidebar: Run details, **Experiment metadata**, methodology assumptions, caveats
- UTC timestamp with click-to-toggle local time
- Export PDF, share link, portable `METHODOLOGY.md` / `README.md` copies

Preview template layout: open `dashboard/dashboard_template.html` in a browser.

## Portfolio mode

Consolidates many runs into one view. Sibling to per-run dashboards — both coexist.

```bash
watermark portfolio . --output ./portfolio.html
# or portfolio-only (no measurement):
watermark --portfolio-dir . --output ./portfolio.html
```

### What you get

- Executive summary + auto-generated insights
- Portfolio totals, attestation fingerprint, snapshot diff vs last regen
- Interactive runs table: search, filters, time range, health column, notes, CSV export
- Charts: regional comparison, per-unit by region, workload breakdown, cost vs carbon, trends
- Auto-generated compare dashboards in `portfolio_compare/` (same workload type + unit count)
- Links to each run's `dashboard.html`

### Auto-refresh

When runs live in the same workspace, `portfolio.html` refreshes after each measurement. New regions are discovered from `summary.json` automatically.

Optional: `WATERMARK_PORTFOLIO_DIR=<workspace>` or `--portfolio-dir` on each run. Skip with `--no-portfolio-update`.

Manual refresh:

```bash
watermark portfolio . --output ./portfolio.html
```

Sort at generation time: `--sort-by timestamp|region|workload|carbon|water|cost|energy`. Additional filters are interactive in the HTML.

### Scan exclusions

Skips `venv/`, `build/`, `dist/`, `__pycache__/`, `.pytest_cache/`, `node_modules/`, paths starting with `.` or `_`, and folders without `summary.json`.

See [`METHODOLOGY.md`](./METHODOLOGY.md) for portfolio aggregation rules and limitations.

## Reference workloads

Fixed prompt sets for reproducibility. Wrap with `watermark`:

| Script | Workload | Default model |
|--------|----------|---------------|
| `generate_images.py` | SDXL image generation | `stabilityai/stable-diffusion-xl-base-1.0` |
| `generate_text.py` | LLM text completion | `meta-llama/Llama-3.2-3B-Instruct` |
| `generate_code.py` | Code completion | `bigcode/starcoder2-3b` |
| `generate_embeddings.py` | Sentence embeddings | `sentence-transformers/all-MiniLM-L6-v2` |

**Text** (use Qwen if Llama is gated: `--model Qwen/Qwen2.5-3B-Instruct` on `generate_text.py`):

```bash
watermark --region eu-north-1 --output ./text_run \
  --run-id text-50-v01 \
  --workload-name "Llama 3 · 50 completions" \
  --model "meta-llama/Llama-3.2-3B-Instruct" \
  --facility-location "Nordic (cloud provider)" \
  --hardware-sku h100-sxm --image-count 50 --seed 42 \
  -- python3 generate_text.py --count 50 --output ./text_run/completions
```

Workload deps (install separately): `torch`, `transformers`, `diffusers`, `sentence-transformers`.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest test_dashboard.py test_portfolio.py -q   # 44 tests
```

`.gitignore` excludes `venv/`, generated image folders (`**/images/`), local measurement run directories, and portfolio artifacts. Commit source code, tests, templates, and docs — not generated image binaries.

## What this is for

1. Run real workloads on real GPU hardware in real regions.
2. Publish `summary.json` + methodology so anyone can audit the numbers.
3. Build a comparison library — same workload across regions, same region across workloads.

## What this is not

Not a production observability agent (no daemon, no per-process attribution in v0.1). Not a hosted SaaS product. Static HTML export is the v0.1 deliverable.

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
