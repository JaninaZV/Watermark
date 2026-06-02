# Watermark artifact schemas

Normative contract for machine-readable outputs. Methodology and computation
details live in [`METHODOLOGY.md`](./METHODOLOGY.md) — this document defines
**shape, versioning, and semantics** only.

Validation helpers: `schema_contract.py` · Contract tests: `test_schema_contract.py`

---

## Run Artifact Schema

### Purpose

`summary.json` is the **public integration API** for a single Watermark
measurement run. Downstream consumers (dashboards, portfolio aggregation, CI
gates, wrappers) should depend on this file — not on CLI stdout or internal
Python modules.

### Current version

```json
"schema_version": "0.3"
```

v0.3 is **enterprise trust**: adds `measurement_grade`, `grade_limiting_factor`,
`methodology_hash`, `per_unit` normalization, operator disclosure provenance,
`gpu_water_fraction`, and `workload_type`. v0.2 parsers should continue across
minor bumps; missing v0.3 fields degrade gracefully with warnings.

v0.2 was **water-first**: WWL, `water_accounting_method`, seasonal stress,
`cooling_type`, and `fetch_water_profile()` seam fields.

### Missing `schema_version`

If `schema_version` is absent or empty, parsers **must** treat the document as
`"0.1"` and emit a warning to stderr:

```
[watermark] warning: summary.json missing schema_version; treating as 0.1 (<context>)
```

Every downstream loader (portfolio, dashboard compare, validators) must behave
consistently. New runs from the meter always write `schema_version`.

### Semver policy (run artifact)

| Change type | Example | Bump |
|-------------|---------|------|
| Documentation only | Clarify field meaning in SCHEMA.md | Patch (`0.1.1` doc tag; output unchanged) |
| Additive | New optional field, new `*_source` enum value, new caveat `code` | Minor (`0.1` → `0.2`) |
| Breaking | Rename/remove field, change units, change carbon energy basis | Major (`1.0`) |

Existing parsers built for `0.1` must continue to work across minor bumps.

### Required top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Contract version (required on new output; see missing-version rule) |
| `run_metadata` | object | Timing, host, scope |
| `measured_sources` | object | CPU/GPU measurement provenance |
| `energy` | object | IT and facility energy totals |
| `carbon` | object | Operational CO₂e |
| `water` | object | Operational water (direct + indirect + stress weighting) |
| `embodied` | object | Amortized manufacturing impacts (may be zero) |
| `lifecycle` | object | Operational + embodied rollups |
| `assumptions` | object | Region, PUE/WUE, grid profile inputs |
| `caveats` | array | Structured methodology limitations (see below) |
| `measurement_grade` | `"A"` \| `"B"` \| `"C"` | v0.3 — worst-condition confidence rollup |
| `grade_limiting_factor` | string \| null | v0.3 — required when grade is B or C |
| `normalization_applied_post_measurement` | boolean | v0.3 — `true` when `per_unit` was applied via `watermark annotate` after the run ended |
| `per_unit` | object \| null | v0.3 — normalized WWL/energy/carbon (see below) |

### `run_metadata`

| Field | Type | Required |
|-------|------|----------|
| `started_at_utc` | string (ISO 8601) | yes |
| `ended_at_utc` | string (ISO 8601) | yes |
| `duration_s` | number | yes |
| `samples` | integer | yes |
| `host_os` | string | yes |
| `rapl_platform` | string \| null | yes |
| `scope` | string (`host` in v0.1) | yes |
| `workload_type` | `training` \| `inference` \| `benchmark` \| `unknown` | v0.3 yes |
| `methodology_hash` | string | v0.3 — `sha256:` digest of METHODOLOGY.md at run time |

### Measurement grades (v0.3)

| Grade | Conditions |
|-------|------------|
| **A** | RAPL/NVML measured + known region + known cooling + water profile with declared `accounting_method` (not `unknown`) + explicit PUE/WUE |
| **B** | Modeled CPU/GPU + known region + no grade-C limiting factors |
| **C** | Any of: `cooling_type_unknown`, `default_pue`, `default_wue`, `rapl_interrupted`, `host_measurement_unavailable`, `region_unknown` |

Grade reflects the **worst** condition during the run (floor, not ceiling). RAPL available
at start but interrupted mid-run → `rapl_interrupted` → grade C.

`grade_limiting_factor` enum: `cooling_type_unknown`, `rapl_interrupted`, `cpu_modeled`,
`gpu_not_measured`, `default_pue`, `default_wue`, `water_accounting_unknown`,
`region_unknown`, `host_measurement_unavailable`.

### `per_unit` (v0.3)

Present when `--token-count`, `--request-count`, `--training-steps`, or `--image-count` is set
at measurement time, or when `watermark annotate` adds normalization afterward.

| Field | Type |
|-------|------|
| `unit_type` | `token` \| `image` \| `request` \| `training_step` |
| `unit_count` | integer |
| `wwl_ml_per_unit` | number |
| `energy_wh_per_unit` | number |
| `carbon_g_per_unit` | number |
| `normalization_source` | string \| null | v0.3 — `cli`, `workload_metrics.json`, `generate_text.py`, `watermark annotate`, etc. |
| `applied_post_measurement` | boolean | v0.3 — mirrors top-level `normalization_applied_post_measurement` |

**Audit semantics:** An annotated `summary.json` is a **derived artifact**, not interchangeable
with the measurement-time summary for attestation of totals. When `watermark annotate` runs,
the CLI writes `summary.pre_annotation.json` once (snapshot before annotation). `watermark
audit-pack` sets `normalization_applied_post_measurement: true` in `audit_manifest.json` and
includes both files when post-hoc normalization applies.

### `measured_sources`

| Field | Type | Description |
|-------|------|-------------|
| `cpu` | string[] | Unique CPU source tags per run |
| `gpu` | string[] | Unique GPU source tags (`["no_gpu"]` when absent) |
| `cpu_rapl_samples` | integer | Samples with RAPL readings |
| `cpu_modeled_samples` | integer | Samples using utilization model |
| `gpu_missing_samples` | integer | Samples without GPU power |

### `energy`

| Field | Type | Description |
|-------|------|-------------|
| `it_cpu_wh` | number | Integrated CPU energy (Wh) |
| `it_cpu_source` | string[] | Source tag(s) for CPU |
| `it_gpu_wh` | number | Integrated GPU energy (Wh) |
| `it_gpu_source` | string[] | Source tag(s) for GPU |
| `it_total_kwh` | number | IT energy total |
| `it_total_source` | string | Always `sum_it_components` |
| `facility_total_kwh` | number | IT × PUE |
| `facility_source` | string | Always `pue_multiplier` |

### `carbon`

| Field | Type | Description |
|-------|------|-------------|
| `co2e_kg` | number | Operational CO₂e (kg) |
| `source` | string | Grid carbon provenance (see enum) |
| `energy_basis_kwh` | number | Facility kWh used as basis |

### `water`

| Field | Type | Description |
|-------|------|-------------|
| `direct_cooling_l` | number | IT kWh × WUE (liters) |
| `indirect_generation_l` | number | Facility kWh × grid water intensity |
| `total_l` | number | Direct + indirect |
| `direct_source` | string | Always `modeled_wue` in v0.1 |
| `indirect_source` | string | Grid water provenance (see enum) |
| `stress_score` | number | WRI Aqueduct 0–1 |
| `stress_level` | string | Human-readable stress band |
| `stress_basin` | string \| null | Basin label |
| `stress_source` | string | Stress data provenance |
| `stress_weighted_total_l` | number | WWL in liters (alias; same as `wwl_l`) |
| `wwl_l` | number | **Watershed-Weighted Liters** — primary water KPI (liters) |
| `wwl_ml` | number | WWL in milliliters (dashboard KPI) |
| `wwl_per_unit_ml` | number \| null | WWL per workload unit when `--image-count` set |
| `water_accounting_method` | string | `consumption`, `withdrawal`, or `unknown` (required) |
| `water_source` | string | Water profile provenance (mirrors carbon `source`) |
| `weighting_methodology` | string | Formula identifier |

### `embodied`

| Field | Type | Description |
|-------|------|-------------|
| `co2e_kg` | number | Amortized manufacturing CO₂e |
| `water_l` | number | Amortized manufacturing water |
| `sku` | string \| null | Resolved hardware SKU |
| `useful_life_hours` | number \| null | Amortization horizon |
| `source` | string | `modeled` or `no_profile` (latter when SKU missing/unknown) |
| `citation` | string \| null | Source reference |
| `label` | string \| null | Human-readable SKU label |

### `lifecycle`

| Field | Type | Description |
|-------|------|-------------|
| `carbon.operational_kg` | number | Operational CO₂e from `carbon.co2e_kg` |
| `carbon.embodied_kg` | number | Amortized manufacturing CO₂e |
| `carbon.total_kg` | number | Operational + embodied |
| `water.operational_l` | number | Operational water from `water.total_l` |
| `water.embodied_l` | number | Amortized manufacturing water |
| `water.total_l` | number | Operational + embodied |

### `assumptions`

| Field | Type | Description |
|-------|------|-------------|
| `region` | string | Region code (e.g. `us-east-1`) |
| `region_label` | string | Display name |
| `grid_source` | string | CLI `--grid-source` (`static`, `electricitymaps`) |
| `grid_profile_source` | string | Profile-level tag (mirrors carbon when unified) |
| `carbon_intensity_source` | string | Carbon provenance tag |
| `carbon_intensity_timestamp` | string \| null | EM timestamp when realtime |
| `em_zone` | string \| null | Electricity Maps zone key |
| `pue` | number | Power usage effectiveness |
| `pue_source` | string | `default_iea_2024` or `user_override` |
| `wue_direct_l_per_kwh` | number | Direct WUE used |
| `wue_source` | string | WUE provenance |
| `grid_co2_kg_per_kwh` | number | Carbon intensity applied |
| `grid_water_l_per_kwh` | number | Indirect water intensity applied |
| `water_source` | string | CLI water profile source (`static`) |
| `water_accounting_method` | string | Consumption vs withdrawal declaration |
| `water_stress_season` | string | `annual`, `winter`, `spring`, `summer`, `fall` |
| `water_stress_as_of` | string \| null | Timestamp for seasonal stress (ISO 8601) |
| `cooling_type` | string | `evaporative`, `air`, `liquid`, `immersion`, `unknown` |
| `carbon_energy_basis` | string | Always `facility_kwh` in v0.1 |
| `carbon_rationale` | string | Scope 2 explanation |
| `cpu_tdp_fallback_w` | number | TDP when RAPL unavailable |
| `hardware_sku` | string \| null | Resolved SKU |
| `hardware_sku_requested` | string \| null | CLI `--hardware-sku` value |

### Source tag enums (`*_source`)

**Grid / carbon**

| Value | Meaning |
|-------|---------|
| `static_avg` | EPA eGRID / IEA annual regional average |
| `em_realtime` | Electricity Maps at run timestamp |

**CPU measurement**

| Value | Meaning |
|-------|---------|
| `rapl_measured` | Linux RAPL hardware counter |
| `modeled_from_util` | psutil utilization × TDP model |

**GPU measurement**

| Value | Meaning |
|-------|---------|
| `nvml_pynvml` | NVML via pynvml |
| `nvml_smi` | nvidia-smi subprocess |
| `no_gpu` | No GPU or no reading |

**Water**

| Value | Meaning |
|-------|---------|
| `modeled_wue` | Direct cooling from WUE × IT kWh |
| `static_avg` | Indirect generation from regional annual table |
| `wri_aqueduct_2023` | WRI Aqueduct Baseline Water Stress (`stress_source` in v0.1) |

**Other**

| Value | Meaning |
|-------|---------|
| `pue_multiplier` | Facility energy = IT × PUE |
| `sum_it_components` | IT total from CPU + GPU |
| `default_iea_2024` | Default PUE constant |
| `user_override` | User passed `--pue` or `--wue` |
| `region_default` | WUE from region profile when CLI did not override |
| `aws_2024_global` | AWS 2024 sustainability report (region profile `wue_source`) |
| `climate_estimate` | Temperate-climate estimate when vendor reports N/A |

### Caveats (structured objects)

Each entry in `caveats` **must** be an object:

```json
{
  "code": "grid_carbon_static_avg",
  "severity": "info",
  "message": "Grid carbon intensity is a regional annual average (static_avg). ..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | yes | Stable machine-readable identifier |
| `severity` | string | yes | `info`, `warning`, or `error` |
| `message` | string | yes | Human-readable explanation |

Caveats are first-class output — not decorative. Parsers may filter by
`severity` or `code`. **New meter output must not use bare strings.**

### Watershed-Weighted Liters (WWL)

Watermark's canonical normalized water unit (analogous to gCO₂e for carbon):

```
WWL = total_operational_water_l × (1 + basin_stress_score)
```

Reported as `water.wwl_l` / `water.wwl_ml`. Gross operational water remains in
`water.total_l` for transparency. WWL is a modest context index — not a precise
social cost of water.

Legacy runs with string caveats may still be read; dashboards normalize them
for display. Validators accept legacy strings only when
`allow_legacy_caveats=True`.

**Known caveat codes (v0.1):** `cpu_rapl_limitation`, `gpu_nvml_limitation`,
`water_annual_average`, `watershed_stress_weighting`, `pue_constant`,
`memory_not_separate`, `embodied_amortized`, `embodied_no_profile`,
`embodied_not_computed`, `grid_carbon_em_realtime`, `grid_carbon_static_avg`,
`grid_intensity_annual_average`, `cpu_modeled_from_util`.

### Water provenance extension (design only — v0.2+)

Carbon and water are **not symmetric**. Future optional water APIs must tag
**methodology and boundary**, not just freshness.

**Boundary enum (binding for future implementations):**

| Value | Meaning |
|-------|---------|
| `facility` | Datacenter cooling loop / on-site water use |
| `grid` | Generation-mix water tied to grid electricity |
| `region` | Regional average spanning multiple facilities/grids |

Planned optional shape (not emitted in v0.1):

```json
"water_provenance": {
  "direct": {
    "method": "wue_multiplier",
    "source": "aws_2024_global",
    "boundary": "facility",
    "citation": "AWS sustainability report 2024"
  },
  "indirect": {
    "method": "grid_intensity",
    "source": "static_avg",
    "boundary": "grid",
    "citation": "NREL Macknick et al. 2012"
  }
}
```

When added, this block will be **optional** (minor version bump). Existing
`water.*` totals and `direct_source` / `indirect_source` tags remain stable.

### Minimal example (`summary.json`)

```json
{
  "schema_version": "0.2",
  "run_metadata": {
    "started_at_utc": "2026-05-30T22:36:13+00:00",
    "ended_at_utc": "2026-05-30T22:38:33+00:00",
    "duration_s": 140.0,
    "samples": 140,
    "host_os": "Linux-6.8-x86_64",
    "rapl_platform": null,
    "scope": "host"
  },
  "measured_sources": {
    "cpu": ["modeled_from_util"],
    "gpu": ["nvml_pynvml"],
    "cpu_rapl_samples": 0,
    "cpu_modeled_samples": 140,
    "gpu_missing_samples": 0
  },
  "energy": {
    "it_cpu_wh": 1.0,
    "it_cpu_source": ["modeled_from_util"],
    "it_gpu_wh": 10.0,
    "it_gpu_source": ["nvml_pynvml"],
    "it_total_kwh": 0.011,
    "it_total_source": "sum_it_components",
    "facility_total_kwh": 0.01738,
    "facility_source": "pue_multiplier"
  },
  "carbon": {
    "co2e_kg": 0.006084,
    "source": "static_avg",
    "energy_basis_kwh": 0.01738
  },
  "water": {
    "direct_cooling_l": 0.00132,
    "indirect_generation_l": 0.033022,
    "total_l": 0.034342,
    "direct_source": "modeled_wue",
    "indirect_source": "static_avg",
    "stress_score": 0.35,
    "stress_level": "medium-high",
    "stress_basin": "Potomac River",
    "stress_source": "wri_aqueduct_2023",
    "stress_weighted_total_l": 0.046362,
    "weighting_methodology": "multiplier_1_plus_score, see methodology"
  },
  "embodied": {
    "co2e_kg": 0.0,
    "water_l": 0.0,
    "sku": null,
    "useful_life_hours": null,
    "source": "no_profile",
    "citation": null,
    "label": null
  },
  "lifecycle": {
    "carbon": {
      "operational_kg": 0.006084,
      "embodied_kg": 0.0,
      "total_kg": 0.006084
    },
    "water": {
      "operational_l": 0.034342,
      "embodied_l": 0.0,
      "total_l": 0.034342
    }
  },
  "assumptions": {
    "region": "us-east-1",
    "region_label": "US Virginia (PJM Mid-Atlantic)",
    "grid_source": "static",
    "grid_profile_source": "static_avg",
    "carbon_intensity_source": "static_avg",
    "carbon_intensity_timestamp": null,
    "em_zone": null,
    "pue": 1.58,
    "pue_source": "default_iea_2024",
    "wue_direct_l_per_kwh": 0.12,
    "wue_source": "region_default",
    "grid_co2_kg_per_kwh": 0.35,
    "grid_water_l_per_kwh": 1.9,
    "carbon_energy_basis": "facility_kwh",
    "carbon_rationale": "Scope 2: grid intensity applied to total facility electricity (IT × PUE).",
    "cpu_tdp_fallback_w": 65.0,
    "hardware_sku": null,
    "hardware_sku_requested": null
  },
  "caveats": [
    {
      "code": "grid_carbon_static_avg",
      "severity": "info",
      "message": "Grid carbon intensity is a regional annual average (static_avg). For real-time attribution use --grid-source electricitymaps."
    }
  ]
}
```

---

## Audit pack (`audit.zip`)

Exported by `watermark audit-pack ./run_dir --output audit.zip`. Self-contained bundle
for compliance review (CSRD-style attestation, third-party audit).

### `audit_manifest.json`

| Field | Type | Description |
|-------|------|-------------|
| `audit_pack_schema_version` | string | Manifest contract version (currently `"1.0"`) |
| `normalization_applied_post_measurement` | boolean | **`true`** when `summary.json` includes post-hoc `per_unit` from `watermark annotate` |
| `artifacts` | object | Map of included files to human-readable role descriptions |
| `pre_annotation_summary_missing` | string | Present only when post-hoc flag is set but `summary.pre_annotation.json` was not found |

When `normalization_applied_post_measurement` is **true**, the zip includes:

| File | Role |
|------|------|
| `summary.pre_annotation.json` | Measurement-time totals and grades **before** annotate |
| `summary.json` | Current summary including derived per-unit intensities |

Auditors must not treat these as the same artifact: pre-annotation attests to what was
known at measurement time; annotated summary adds unit counts supplied afterward.

---

## Portfolio Schema

### Purpose

Portfolio HTML embeds a JSON payload (`WATERMARK_PORTFOLIO_DATA`) built by
`dashboard/portfolio.py`. This is a **separate integration surface** from
per-run `summary.json`, with its own version and semver policy.

### Current version

```json
"schema_version": "0.1-portfolio"
```

Portfolio payloads always include `schema_version` (no missing-version rule).

### Semver policy (portfolio)

Independent of the run artifact schema.

| Change type | Example | Bump |
|-------------|---------|------|
| Documentation only | Clarify `insights[].type` | Patch |
| Additive | New chart array, new optional run row field | Minor (`0.1-portfolio` → `0.2-portfolio`) |
| Breaking | Rename/remove top-level keys, change aggregation units | Major (`1.0-portfolio`) |

Run artifact minor bumps do **not** automatically bump portfolio version.
Portfolio code must tolerate new fields in loaded `summary.json` files.

### Required top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Portfolio contract version |
| `scan_dir` | string \| null | Root directory scanned |
| `sort_by` | string | Default sort key |
| `meta` | object | Generation metadata |
| `summary` | object | Aggregated totals |
| `executive_summary` | string[] | Narrative bullets |
| `insights` | array | Typed insight objects |
| `attestation` | object | Fingerprint and scan metadata |
| `snapshot_diff` | object \| null | Change since last snapshot |
| `runs` | array | Per-run table rows (derived from `summary.json`) |
| `regional_chart` | array | Regional aggregation series |
| `regional_per_unit_chart` | array | Per-unit regional series |
| `workload_chart` | array | Workload aggregation series |
| `cost_carbon_chart` | array | Cost vs carbon series |
| `trends` | object | Time-series rollups |
| `compare_links` | object | Map of compare pair keys to HTML paths |

### `meta`

| Field | Type | Description |
|-------|------|-------------|
| `generated_at_utc` | string | ISO 8601 UTC timestamp |
| `methodology_version` | string | Package methodology version |
| `methodology_href` | string \| null | Relative link to METHODOLOGY.md |
| `total_runs_scanned` | integer | All runs discovered |
| `portfolio_href` | string \| null | Output HTML filename |

### `summary` (portfolio aggregation)

| Field | Type | Description |
|-------|------|-------------|
| `run_count` | integer | Measurement runs included in totals |
| `total_facility_wh` | number | Sum of facility Wh |
| `total_carbon_g` | number | Sum of carbon (grams) |
| `total_water_ml` | number | Sum of water (milliliters) |
| `total_cost_usd` | number \| null | Sum of compute cost when available |
| `date_range` | object \| null | `{earliest, latest}` ISO dates |
| `regions` | string[] | Unique region codes |
| `workloads` | string[] | Unique workload names |
| `hardware_skus` | string[] | Unique hardware SKU labels |

### `attestation`

| Field | Type | Description |
|-------|------|-------------|
| `scan_dir` | string \| null | Root directory scanned |
| `runs_scanned` | integer | All runs discovered |
| `measurement_runs` | integer | Runs counted as measurements |
| `data_fingerprint` | string | SHA-256 prefix over run IDs + totals |
| `generated_at_utc` | string \| null | Generation timestamp |
| `methodology_version` | string | Package methodology version |

### Relationship to run artifact schema

Portfolio **reads** per-run `summary.json` files and **derives** row objects.
It does not recompute impact math from raw samples. When run schema adds
fields, portfolio loaders should forward-compatible ignore unknown keys.

Shared semantics (region codes, `*_source` tags, carbon/water units) are
defined in the Run Artifact Schema section above.

### Minimal example (payload excerpt)

```json
{
  "schema_version": "0.1-portfolio",
  "scan_dir": "/data/experiments",
  "sort_by": "timestamp",
  "meta": {
    "generated_at_utc": "2026-06-01T12:00:00Z",
    "methodology_version": "0.1.0",
    "methodology_href": "./METHODOLOGY.md",
    "total_runs_scanned": 2,
    "portfolio_href": "portfolio.html"
  },
  "summary": {
    "run_count": 2,
    "total_facility_wh": 98.8,
    "total_carbon_g": 18.8,
    "total_water_ml": 215.0,
    "regions": ["us-east-1", "eu-north-1"],
    "date_range": { "earliest": "2026-05-30", "latest": "2026-05-31" }
  },
  "executive_summary": [],
  "insights": [],
  "attestation": {
    "scan_dir": "/data/experiments",
    "runs_scanned": 2,
    "measurement_runs": 2,
    "data_fingerprint": "abc123",
    "generated_at_utc": "2026-06-01T12:00:00Z",
    "methodology_version": "0.1.0"
  },
  "snapshot_diff": null,
  "runs": [],
  "regional_chart": [],
  "regional_per_unit_chart": [],
  "workload_chart": [],
  "cost_carbon_chart": [],
  "trends": {},
  "compare_links": {}
}
```

---

## Comparison Artifact Schema (`comparison.json`)

### Current version

```json
"schema_version": "0.1-comparison"
```

Produced by `watermark compare-regions`. Holds per-region projected footprints for the
same reference IT energy — designed for carbon/water inversion analysis.

| Field | Type | Description |
|-------|------|-------------|
| `workload` | string | Reference workload key (`embeddings`, `images`, `text`, `benchmark`) |
| `reference_energy` | object | `it_kwh`, `pue`, `cooling_type`, `water_source`, `grid_source` |
| `regions` | array | Per-region `facility_wh`, `carbon_g`, `wwl_ml`, `measurement_grade` |
| `winners` | object | `lowest_carbon_region`, `lowest_wwl_region` |
| `water_carbon_inversion` | boolean | true when lowest-carbon ≠ lowest-WWL region |
| `tradeoff_narrative` | string \| null | Machine-readable ratio narrative |
| `assumptions_consistent` | boolean | false if water/carbon sources differ across regions |
