# Watermark Meter

**Measure compute's water cost — direct, indirect, and watershed-weighted — with every assumption auditable.**

> **Status:** v0.2 · water-first · methodology-first · public preview

Watermark is an open-source CLI that meters AI/compute workloads and answers a
question most footprint tools skip: **what did this job cost in water, and was
that water drawn from a stressed basin?** It measures energy (RAPL/NVML) and
models water in two channels — datacenter cooling (WUE) and electricity
generation — then reports **Watershed-Weighted Liters (WWL)** as the canonical
water unit. Carbon is included for context, especially the carbon–water tradeoff
(e.g. Nordic hydropower: low carbon, high indirect water).

**Quick start**

```bash
pip install -e .
watermark --help
```

Full methodology: [`METHODOLOGY.md`](./METHODOLOGY.md) · Schema: [`SCHEMA.md`](./SCHEMA.md) · License: [Apache 2.0](./LICENSE)

---

## Why water first

Every "green AI" story picks a region for low carbon. **Watermark shows what that
choice costs in water.** Sweden can be 8× cleaner on carbon than Virginia but
2× higher on watershed-weighted water for the same measured energy — because
hydropower grids carry large indirect water footprints.

```bash
# Hero comparison — same workload, three regions
watermark --region us-east-1 --duration 60 --output ./run_va
watermark --region eu-north-1 --duration 60 --output ./run_se
watermark --region us-west-1 --duration 60 --output ./run_or
watermark portfolio ./experiments --output portfolio.html
```

You optimized for carbon. **Did you check water?**

---

## What's in scope for v0.2

- **Energy** — CPU (RAPL or utilization×TDP) + GPU (NVML / nvidia-smi)
- **Water (primary)** — direct cooling (WUE) + indirect generation water → **WWL**
- **Watershed stress** — WRI Aqueduct basin weighting (seasonal uplift where modeled)
- **Cooling type** — `--cooling-system` adjusts direct WUE (evaporative, air, liquid, immersion)
- **Carbon (context)** — operational CO₂e; optional ElectricityMaps realtime
- **Embodied impact** — optional amortized manufacturing water/carbon via `--hardware-sku`
- **Comparison** — regional scenarios + portfolio with carbon vs WWL scatter
- **Audit trail** — `summary.json`, `measurements.csv`, `dashboard.html`

## What's not yet supported

- Direct water metering (no flow sensor — energy × coefficients, honestly labeled)
- Live water intensity APIs (architecture seam exists; static tables default)
- Operator disclosure ingestion pipeline
- Per-process attribution (planned v0.3)

---

Unlike carbon-only calculators, Watermark:

- splits water into **direct cooling** (regional WUE) and **indirect generation** (grid mix)
- reports **Watershed-Weighted Liters (WWL)** — gross water × (1 + basin stress score)
- surfaces the **carbon–water tradeoff** when comparing cloud regions
- adjusts direct WUE when you pass `--cooling-system` (operator knowledge beats any API)
- reads CPU/GPU **energy** from hardware where possible; water is modeled honestly, not faked
- tags every line with source and emits structured caveats (`summary.json`)

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

### Per-run dashboard (v0.2)

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
