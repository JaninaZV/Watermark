# Watermark Meter — Methodology v0.1

This document explains exactly how `watermark_meter.py` produces the numbers it produces, what is measured directly from hardware, what is modeled, and what should not be claimed from a single run. It is meant to be auditable: anyone reading this should be able to reproduce or challenge any figure the tool reports.

## What the meter actually measures

The meter samples the host on a fixed interval (default one second) and records per-sample readings in `measurements.csv`. Each sample includes an `elapsed_s` field — the actual wall time between reads, measured with a monotonic clock — so power and energy integration account for scheduler jitter rather than assuming a perfect interval.

**CPU package energy** is read from the Linux RAPL (Running Average Power Limit) interface when accessible:

- Intel: `/sys/class/powercap/intel-rapl:N/energy_uj`
- AMD: `/sys/class/powercap/amd-rapl:N/energy_uj` (kernel 5.11+)

These counters report cumulative package energy in microjoules. Power in watts is computed as the delta between two reads divided by `elapsed_s`. On multi-socket hosts, package-level zones from all sockets are summed. Counter wrap-around is handled using each zone's `max_energy_range_uj`; if a counter decreases without a known maximum, that sample is discarded rather than producing a spurious spike.

Only zones named `package`, `package-0`, `package-1`, or names beginning with `package` are included. Subzones (e.g. `intel-rapl:0:0` for DRAM) are excluded to avoid double-counting.

This is a direct hardware reading on supported Linux bare-metal systems — the same source Kepler, Scaphandre, and PowerAPI use. It is tagged `rapl_measured` in the CSV. The summary records which CPU vendor was detected (`rapl_platform`: `intel` or `amd`).

**GPU power** is read from NVIDIA NVML through the `pynvml` Python bindings (`nvml_pynvml`), or by shelling out to `nvidia-smi --query-gpu=power.draw` when pynvml is not installed (`nvml_smi`). NVML reports instantaneous board power in milliwatts. The meter sums across all visible GPUs. Lines reporting `[N/A]` or `[Not Supported]` are skipped. When no GPU is present, samples are tagged `no_gpu`.

**CPU and memory utilization** are read via `psutil` and recorded in the CSV. They are not used to compute energy when RAPL is available. They appear for sanity-checking and for the fallback model when RAPL is unavailable.

## What the meter models

Several quantities cannot be measured at the host level and must be modeled from regional averages or published constants. Every output carries a specific source tag (not just "measured" vs "modeled") so a reviewer can see exactly which methodology produced each number.

### Source tag vocabulary

| Tag | Meaning |
|-----|---------|
| `rapl_measured` | CPU energy from RAPL hardware counters |
| `modeled_from_util` | CPU energy from utilization × TDP model |
| `nvml_pynvml` | GPU power from pynvml / NVML |
| `nvml_smi` | GPU power from nvidia-smi subprocess |
| `no_gpu` | No GPU detected or no reading for this sample |
| `pue_multiplier` | Facility energy = IT energy × PUE |
| `sum_it_components` | IT total = CPU Wh + GPU Wh |
| `sum_water_components` | Water total = direct + indirect |
| `modeled_annual_average` | Grid carbon/water from static regional table |
| `modeled_wue` | Direct cooling water from WUE constant |
| `default_iea_2024` | Default PUE (1.58) from IEA Electricity 2024 |
| `default_vendor_avg` | Default WUE from unweighted AWS/Azure/Google average |
| `user_override` | User supplied `--pue` or `--wue` on the command line |

### CPU energy when RAPL is unavailable

On macOS, Windows, and most cloud VMs, RAPL is either absent or blocked. In those cases the meter falls back to a utilization-times-TDP model:

```
idle_draw + (TDP − idle_draw) × utilization
```

Idle draw defaults to 30% of TDP. This is a known-weak approximation; samples are tagged `modeled_from_util`. When any samples use this model, the summary adds a caveat noting the assumed TDP and sample count, and recommends verifying against the instance SKU or SPECpower for publishable results.

If RAPL is available at startup but individual reads fail mid-run (permission error, counter reset), those samples also fall back to `modeled_from_util`. The summary records both source types and per-source sample counts in `measured_sources`.

### Cooling and facility overhead (PUE)

Hardware measures IT energy only. Real facility energy includes cooling, lighting, UPS losses, and so on. PUE is the ratio of total facility energy to IT energy:

```
facility_kwh = it_kwh × PUE
```

The meter defaults to PUE 1.58, the IEA 2024 global weighted average (`default_iea_2024`). Hyperscaler-claimed PUEs are lower (around 1.10–1.20), but third-party audits of those numbers are limited; we use the IEA figure as a conservative default. PUE varies with weather, load, and time of day; an annual-average multiplier is therefore an approximation. Facility energy is tagged `pue_multiplier` in the output.

### Grid carbon intensity

The meter maps a region code (e.g. `us-east-1`) to an annual-average operating CO₂e intensity in kilograms per kWh via `fetch_grid_profile()`. In v0.1 this calls a static lookup (`modeled_annual_average`). Sources are EPA eGRID 2022 for US subregions and IEA 2024 country averages for international regions.

Carbon is computed as:

```
co2e_kg = facility_kwh × grid_co2_kg_per_kwh
```

Grid intensity is applied to **facility** electricity (IT × PUE), not IT energy alone. This follows GHG Protocol Scope 2 location-based accounting: the data center draws grid power for both compute and cooling. The summary records `carbon_energy_basis: facility_kwh` and the rationale explicitly.

These are *annual averages* — actual carbon intensity varies hour to hour depending on grid dispatch. For accurate attribution at a specific time, use a real-time provider; v0.2 will replace `fetch_grid_profile()` with ElectricityMaps or WattTime. The static table is a starting credible value, not a final number. Short workloads may not align with marginal dispatch at execution time; this is noted in the caveats.

### Grid profile vs physical facility location

Region codes (e.g. `eu-north-1`) select a **grid profile** — carbon intensity, water intensity, WUE, and watershed stress for that cloud region's published geography. They do not automatically know where rented bare-metal GPU infrastructure is physically located.

In v0.1, `eu-north-1` maps to the **Sweden** profile (AWS Stockholm / Nordic low-carbon grid). If you measure on Norwegian infrastructure but pass `--region eu-north-1`, the tool applies Sweden's grid factors — a reasonable v0.1 approximation because Nordic grids are similar, but not identical to Norway's.

For publishable runs, record the physical site via `--facility-location` or in `workload.json` as `facility_location` (e.g. `"Norway (RunPod)"`, `"Iceland (RunPod EUR-IS-3)"`). The `--region` flag selects the grid profile only. The per-run dashboard shows an **Experiment metadata** sidebar section when `model`, `facility_location`, `cooling_system`, or `notes` are set, and a methodology warning when facility location differs from the grid profile label. In external write-ups, describe Nordic comparisons as **Nordic** unless the facility is confirmed in Sweden. Norway- and Iceland-specific grid profiles are planned for v0.2.

### Experiment metadata (`workload.json`)

Separate from `summary.json`. Written when you pass experiment flags on the CLI; omitted keys are not stored (never `null` placeholders).

| Key | CLI flag | Purpose |
|-----|----------|---------|
| `run_id`, `name`, `image_count`, `hardware`, `steps`, `seed`, `compute_rate_usd_hr` | existing flags | Workload identity and units |
| `model` | `--model` | Model ID or label (e.g. `stabilityai/stable-diffusion-xl-base-1.0`) |
| `facility_location` | `--facility-location` | Physical datacenter (free text) |
| `cooling_system` | `--cooling-system` | Cooling type if known (e.g. `evaporative`) — omit if unknown |
| `notes` | `--notes` | Free-form run notes |

Regenerate dashboards without re-measuring: `python -m dashboard ./run_dir` (reads existing `workload.json`; CLI overrides merge for one-off regen).

### Optional embodied impact (hardware SKU)

When `--hardware-sku` is set (e.g. `h100-sxm`, `a100`), the meter amortizes published manufacturing carbon and water over a useful life and surfaces **lifecycle** totals on the dashboard alongside operational footprint. Tagged `modeled, amortized` with SKU-specific citations. This is a v0.1 preview — not a full ISO-compliant product LCA.

### Water consumption

Watermark does **not** read a flow meter. It models water from measured energy using
published coefficients — and tags whether each component is direct (facility) or
indirect (grid).

#### Consumption vs withdrawal

Datacenters often report **withdrawal** (water drawn from a source) and
**consumption** (water evaporated or otherwise not returned) as different numbers.
They are not comparable. Watermark's modeled totals are **consumption-equivalent**
volumes unless an operator source declares otherwise. Every run records
`water_accounting_method` (`consumption`, `withdrawal`, or `unknown`; default
`unknown`). Treat `unknown` as a warning — the value may over- or under-state
facility impact depending on how the site reports water.

#### Watershed-Weighted Liters (WWL)

Carbon has gCO₂e as a normalized unit. Watermark's canonical water unit is **WWL**
(Watershed-Weighted Liters):

```
WWL = total_operational_water_l × (1 + basin_stress_score)
```

WWL combines direct + indirect operational water with a modest WRI Aqueduct
baseline stress multiplier. It is a **context index**, not a precise social cost
of water. Gross liters remain in output for transparency.

Water is split into two components with **different energy bases**, because WUE and
generation-water intensity are defined against different denominators in the source literature.

**Direct cooling water** — on-site water evaporated or consumed for cooling — uses WUE (Water Usage Effectiveness), expressed in liters per kWh of **IT energy**:

```
direct_cooling_l = it_kwh × WUE
```

The default WUE of 0.59 L/kWh is the unweighted average of published 2023 values from AWS (~0.18), Microsoft Azure (~0.49), and Google Cloud (~1.10). Tagged `modeled_wue`. WUE is highly site-specific; for real attribution use the operator's published facility-level WUE.

**Indirect generation water** — freshwater consumed producing the electricity the facility draws from the grid — uses the regional lookup in liters per kWh of **facility electricity**:

```
indirect_generation_l = facility_kwh × grid_water_l_per_kwh
```

This draws from NREL Macknick et al. 2012 and USGS 2020 thermoelectric water-use estimates, combined with each region's grid mix. Tagged `modeled_annual_average`.

**Total water:**

```
total_l = direct_cooling_l + indirect_generation_l
```

Tagged `sum_water_components`.

### Watershed-stress weighting

Gross water totals treat every litre equally, but a litre withdrawn in a drought-stressed basin has greater local impact than the same litre in a water-abundant one. Watermark surfaces this qualitative dimension using **WRI Aqueduct Baseline Water Stress (BWS)** data, versioned as `wri_aqueduct_2023` in `REGION_PROFILES`.

Each region entry includes:

- **`water_stress_score`** — a normalized multiplier component from 0.0 (low stress) to 1.0 (extremely high), mapped from Aqueduct BWS categories
- **`water_stress_level`** — categorical label (`low`, `low-medium`, `medium`, `high`, `extremely-high`) for dashboard badges
- **`water_stress_basin`** — named hydrological basin (e.g. Lower Chesapeake, Lake Mälaren, Krishna)
- **`stress_source`** — always `wri_aqueduct_2023` unless overridden in a future methodology revision

**Stress-weighted total:**

```
stress_weighted_total_l = total_l × (1 + stress_score)
```

The multiplier is intentionally modest: at `stress_score = 1.0` (extremely high), gross water doubles. The goal is to flag relative basin context, not to claim a precise social cost of water.

**Limitations:**

- Aqueduct data is **basin-level**, not facility-level. A data center may draw from a municipal supply, recycled water, or a sub-basin not captured in the coarse Aqueduct polygon assigned to the cloud region.
- Stress scores are **static** — they do not reflect seasonal drought, year-over-year Aqueduct updates, or real-time reservoir levels.
- The weighting applies to **operational water only** (direct + indirect). Embodied manufacturing water is not stress-weighted in v0.1.
- Indirect generation water is attributed to the **grid region**, not to the power plant's local watershed — a known simplification.

Tagged `multiplier_1_plus_score, see methodology` in `summary.json` under
`water.weighting_methodology`. The primary KPI field is `water.wwl_ml`.

**Seasonal stress (v0.2):** basin stress may be uplifted by season using static
regional multipliers (run timestamp → meteorological season). Recorded in
`assumptions.water_stress_season` and `assumptions.water_stress_as_of`.

**Cooling type (v0.2):** `--cooling-system` scales direct WUE (evaporative = 1×
regional default; air/dry ≈ 0.35×; liquid/immersion ≈ 0.08–0.10×). Stored as
`assumptions.cooling_type`.

### Water data architecture

Carbon has ElectricityMaps as a de facto live reference. **Water has no equivalent
yet.** Watermark still provides `fetch_water_profile()` — the same progressive
enhancement seam as `fetch_grid_profile()`:

- `static` (default): regional WUE + Aqueduct + seasonal multipliers, offline
- `operator` (reserved): facility-reported disclosures with explicit accounting method

This is intentional: the seam exists before the APIs do.

## Aggregation pipeline

All impact math runs through a single pure function, `compute_impacts()`, which takes IT kWh, facility kWh, the grid profile, and WUE, and returns carbon and water totals with source tags. Dashboard and portfolio behavior (including impact display) is covered by `test_dashboard.py` and `test_portfolio.py`.

Energy integration over the run:

```
total_cpu_wh = Σ (cpu_watts × elapsed_s / 3600)
total_gpu_wh = Σ (gpu_watts × elapsed_s / 3600)    # missing GPU samples excluded
it_kwh       = (total_cpu_wh + total_gpu_wh) / 1000
facility_kwh = it_kwh × PUE
```

GPU samples where `gpu_watts` is `None` (transient NVML failure) are excluded from the GPU total; the summary reports `gpu_missing_samples` so a reviewer can assess undercount risk.

## Output schema

Each run writes three files to the `--output` directory.

### measurements.csv

Per-sample readings. Key columns:

| Column | Description |
|--------|-------------|
| `timestamp` | UTC ISO 8601 |
| `interval_s` | Configured sampling interval |
| `elapsed_s` | Actual elapsed seconds since previous sample |
| `cpu_watts` | CPU power estimate for this interval |
| `cpu_source` | `rapl_measured` or `modeled_from_util` |
| `gpu_watts` | GPU power (null if unavailable this sample) |
| `gpu_source` | `nvml_pynvml`, `nvml_smi`, or `no_gpu` |
| `cpu_percent` | psutil CPU utilization (diagnostic) |
| `memory_percent` | psutil memory utilization (diagnostic) |

### summary.json

Aggregated totals and all assumptions. Key sections:

- **`run_metadata`** — duration, sample count, host OS, `rapl_platform`, measurement `scope` (host-only in v0.1)
- **`measured_sources`** — list of CPU/GPU source tags seen, plus `cpu_rapl_samples`, `cpu_modeled_samples`, `gpu_missing_samples`
- **`energy`** — IT and facility kWh with per-component source tags
- **`carbon`** — CO₂e kg, `source`, `energy_basis_kwh`
- **`water`** — direct, indirect, total liters with `direct_source` and `indirect_source`
- **`assumptions`** — region, PUE/WUE values and their provenance (`pue_source`, `wue_source`, `grid_profile_source`), carbon energy basis and rationale, CPU TDP fallback
- **`caveats`** — methodology limitations, including dynamic caveats when CPU modeling was used

### report.md

Human-readable summary mirroring `summary.json`. Every line carries the specific source tag(s) that produced the number, e.g. `rapl_measured: 200 samples`, `modeled_annual_average`, `pue_multiplier, default_iea_2024`.

## What the meter does not include (v0.1 limits)

Per-process attribution (e.g. isolating one training job from system background load) is not supported. The `--scope host` flag is the only option; `--scope pid` is planned for v0.3 via eBPF (Kepler-style).

The meter does not separately model memory (DRAM) power, network equipment, or storage beyond what RAPL package counters partially capture. For storage- or network-heavy workloads, energy may be understated.

Water stress weighting uses static WRI Aqueduct basin scores — not facility-level intake or seasonal drought.

A full supply-chain LCA (full embodied scope, e-waste, land use) is not claimed. Optional `--hardware-sku` embodied amortization is a simplified preview only.

## How to use a single run

A single run produces a defensible estimate of the energy, carbon, and water consumed by one specific workload on one specific host, given explicit assumptions recorded in `summary.json`. It does not produce a universal "true cost" — that depends on the data center, grid, time of day, season, cooling system, and supply chain.

The most useful first experiments are comparisons rather than absolutes: the same workload in two regions, or two model architectures in the same region. Comparisons are robust to much of the modeling uncertainty because shared assumptions (PUE, regional intensity) cancel.

Runs shorter than one sampling interval, or where the wrapped command exits before the first sample, produce no output — the meter raises an error rather than writing empty summaries.

## What changes for v0.2

Three improvements would move this from "starter artifact" to "publishable methodology."

**Real-time grid intensity.** Replace the body of `fetch_grid_profile()` with an ElectricityMaps or WattTime API call. The function signature and output schema already accept a `timestamp` and `source` field; v0.2 fills them with live data while keeping the static table as fallback.

**Embodied carbon and water (deeper LCA).** Extend `--hardware-sku` profiles with fuller supply-chain data and uncertainty ranges.

**Cloud instance fingerprinting.** When RAPL is blocked in a VM, look up published power profiles for the detected instance type rather than generic TDP.

**Country-specific grid profiles.** Norway, Iceland, and other RunPod sites currently share nearest AWS-region proxies (e.g. `eu-north-1` for Nordic).

## What changes for v0.3

**Per-process attribution** via eBPF (following the CNCF Kepler approach) behind a `--scope pid` flag, using the `EnergyProbe` interface already defined in the script.

**Production agent** packaging (multi-file, daemon mode, compliance reporting) while preserving the audit trail format established here.

## Portfolio aggregation

Portfolio mode (`watermark portfolio <dir> --output portfolio.html`, or `watermark --portfolio-dir <dir> --output portfolio.html`) scans a directory tree for existing `summary.json` files and renders a single HTML dashboard. It does **not** re-run the meter or recompute impact math from raw samples.

### Auto-refresh after measurements

When a measurement completes, Watermark can regenerate `portfolio.html` automatically if it finds a portfolio workspace: the parent directory already contains `portfolio.html` or other runs, or you set `--portfolio-dir` / `WATERMARK_PORTFOLIO_DIR`. Use `--no-portfolio-update` to skip. New runs in new regions are discovered from `summary.json` without configuration.

### What the portfolio aggregates

The portfolio header sums values already stored in each run's `summary.json` (measurement runs only — short debug stubs are excluded from totals):

- **Run count**, **total facility energy** (Wh), **operational carbon** (g CO₂e), **operational water** (mL), **compute cost** (USD when recorded)
- **Date range**, **unique regions**, **workloads**, **hardware SKUs**
- **Executive summary** bullets and **insights** (auto-generated: regional spread, cleanest/dirtiest region, recommendations)
- **Attestation** fingerprint and **snapshot diff** vs previous regen (`portfolio.snapshot.json` sidecar)

These are simple sums over finalized run totals. Useful for inventory ("what have I measured?") — not a controlled experiment unless workloads are comparable.

### What the portfolio compares without aggregating

- **Regional comparison** — carbon and water by measured region; per-unit chart when unit counts exist
- **Workload comparison** — totals and per-unit metrics by workload label (images, text completions, embeddings, etc.)
- **Cost vs carbon** — scatter-style comparison across measurement runs
- **Trend over time** — facility energy for repeated workload+region pairs
- **Side-by-side compare dashboards** — auto-generated in `portfolio_compare/` for pairs with same workload type and unit count; insight cards link through

The interactive runs table supports search, dropdown filters (with counts), time-range filter, sort, health indicator, notes column, CSV export of visible rows, and links to per-run dashboards.

### Limitations

- **No re-computation of historical data.** Portfolio reads `summary.json` only. Regenerate per-run outputs if regional constants change.
- **No cross-run normalization** in header totals — different hardware/durations are summed naively.
- **Static HTML view layer** — regenerated on demand; `portfolio.snapshot.json` tracks last regen for diff only.
- **Scan boundaries.** Skips `venv/`, `build/`, `__pycache__`, `.pytest_cache/`, paths starting with `.` or `_`, and folders without `summary.json`.

### Audit trail

Per-run audit data is unchanged. Each run directory still holds `measurements.csv`, `summary.json`, `report.md`, and optional `workload.json` as the source of truth. The portfolio is a **view layer** — it links to per-run dashboards for drill-down and does not replace publishing individual run folders for external review.

## Citations

The principal sources behind the constants in this tool are:

- IEA, "Electricity 2024" — global data center PUE and total consumption figures.
- EPA eGRID 2022 — US subregional grid carbon intensity.
- IEA country-level CO₂ emission factors for electricity, 2024 edition — non-US regions.
- Macknick et al., "Operational water consumption and withdrawal factors for electricity generating technologies" (NREL 2012), and USGS Circular 1513 on US thermoelectric water use (2020) — generation water intensity.
- AWS (2023), Microsoft (2023), Google (2023) sustainability reports — WUE.
- Hähnel et al., "Measuring energy consumption for short code paths using RAPL" (2012); subsequent validation by Khan et al. and others — RAPL interface.
- CNCF Kepler — audit-grade reference for eBPF-based per-process energy attribution.

## Verification

Critical math and dashboard/portfolio behavior are covered by `test_dashboard.py` and `test_portfolio.py` (44 tests). Dev install: `pip install -e ".[dev]"`. Run:

```
python3 -m pytest test_dashboard.py test_portfolio.py -q
```
