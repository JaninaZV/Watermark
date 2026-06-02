#!/usr/bin/env python3
"""
watermark_meter.py
==================

Workload-level energy, carbon, and water meter for AI / compute workloads.

What this measures (when possible):
  - CPU package energy via Intel/AMD RAPL counters (Linux bare metal)
  - GPU power via NVIDIA NVML (pynvml) or nvidia-smi fallback

What this models (because real measurement isn't available):
  - Cooling/facility overhead via PUE
  - Grid carbon intensity (kgCO2e/kWh), looked up by region with cited sources
  - Water consumption (L/kWh), split into:
      * Direct cooling water (WUE), vendor-published
      * Indirect generation water, NREL/USGS thermoelectric averages

The goal is to produce numbers a reviewer can audit, not numbers that look
authoritative. Every assumption is recorded in the output summary.

Outputs (written to --output dir):
  - measurements.csv : per-sample readings
  - summary.json     : aggregated totals + all assumptions used
  - report.md        : human-readable summary

Usage:
  Standalone, measure for 60 seconds:
    python watermark_meter.py --duration 60 --region us-east-1 --output ./run1

  Wrap a command (measure while it runs):
    python watermark_meter.py --region eu-west-1 --output ./run1 -- python train.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Methodology constants (with citations in comments)
# ---------------------------------------------------------------------------

# Power Usage Effectiveness: total facility energy / IT energy.
# IEA "Electricity 2024" report, global data centre weighted average ~1.5-1.58.
DEFAULT_PUE = 1.58

# Direct Water Usage Effectiveness: liters of on-site water per kWh of IT energy.
# Per-region values live in REGION_PROFILES (AWS 2024 sustainability report, etc.).
# DEFAULT_WUE_DIRECT is the global-avg fallback when --wue is not overridden.
DEFAULT_WUE_DIRECT = 0.15  # AWS global WUE 2024 (sustainability.aboutamazon.com/products-services/aws-cloud)

# WRI Aqueduct Baseline Water Stress — stress weighting metadata version.
STRESS_SOURCE_DEFAULT = "wri_aqueduct_2023"
WATER_STRESS_WEIGHTING_METHOD = "multiplier_1_plus_score, see methodology"
WATER_SOURCE_STATIC = "static_avg"
WATER_SOURCE_OPERATOR = "operator_disclosure"
WATER_ACCOUNTING_UNKNOWN = "unknown"
WATER_ACCOUNTING_CONSUMPTION = "consumption"
WATER_ACCOUNTING_WITHDRAWAL = "withdrawal"

COOLING_WUE_MULTIPLIERS = {
    "evaporative": 1.0,
    "air": 0.6,
    "dry": 0.6,
    "liquid": 0.2,
    "immersion": 0.2,
    "unknown": 1.0,
}

DEFAULT_SEASONAL_STRESS_MULTIPLIERS = {
    "annual": 1.0,
    "winter": 0.85,
    "spring": 0.95,
    "summer": 1.25,
    "fall": 1.0,
}

# Seasonal stress uplift for drought-prone basins (multiplier on Aqueduct annual score).
REGION_SEASONAL_STRESS_MULTIPLIERS: dict[str, dict[str, float]] = {
    "us-west-1": {"summer": 1.45, "fall": 1.15},
    "us-west-2": {"summer": 1.20},
    "ap-south-1": {"summer": 1.35, "spring": 1.10},
    "ap-southeast-1": {"summer": 1.15},
}

# Per-region grid intensities and direct cooling WUE.
#   co2_kg_per_kwh : annual average operating CO2e intensity
#       sources: EPA eGRID 2022 (US subregions); IEA 2024 country averages (intl).
#   water_l_per_kwh : indirect water consumed in electricity generation
#       sources: NREL Macknick et al. 2012 + USGS thermoelectric water use 2020
#   wue_direct_l_per_kwh : direct cooling water per kWh IT (vendor-published where available)
#       sources: AWS 2024 Sustainability Report regional WUE table (primary);
#       climate_estimate where AWS reports N/A for that region.
#
# Values for sa-east-1, ca-central-1, ap-southeast-2, ap-east-1, and ap-northeast-2
# are based on national/regional averages. Facility-specific values may vary. v0.2 will
# refine with operator-published data where available.
REGION_PROFILES = {
    # WRI Aqueduct 2023 BWS: low–medium; basin ~Northern Virginia / Lower Chesapeake.
    "us-east-1": {
        "label": "US Virginia (PJM Mid-Atlantic)",
        "co2_kg_per_kwh": 0.35,
        "water_l_per_kwh": 1.90,
        "wue_direct_l_per_kwh": 0.12,
        "wue_source": "aws_2024_virginia",
        "water_stress_score": 0.25,
        "water_stress_level": "low-medium",
        "water_stress_basin": "Lower Chesapeake",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: low–medium; Ohio River basin.
    "us-east-2": {
        "label": "US Ohio (RFC East)",
        "co2_kg_per_kwh": 0.43,
        "water_l_per_kwh": 1.80,
        "wue_direct_l_per_kwh": 0.10,
        "wue_source": "aws_2024_ohio",
        "water_stress_score": 0.25,
        "water_stress_level": "low-medium",
        "water_stress_basin": "Ohio River",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: high; Sacramento–San Joaquin basin.
    "us-west-1": {
        "label": "US N. California (CAISO)",
        "co2_kg_per_kwh": 0.20,
        "water_l_per_kwh": 1.50,
        "wue_direct_l_per_kwh": 0.51,
        "wue_source": "aws_2024_n_california",
        "water_stress_score": 0.85,
        "water_stress_level": "high",
        "water_stress_basin": "Sacramento-San Joaquin",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: low; Columbia River basin.
    "us-west-2": {
        "label": "US Oregon (NWPP)",
        "co2_kg_per_kwh": 0.11,
        "water_l_per_kwh": 4.20,
        "wue_direct_l_per_kwh": 0.16,
        "wue_source": "aws_2024_oregon",
        "water_stress_score": 0.0,
        "water_stress_level": "low",
        "water_stress_basin": "Columbia River",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: low; Eastern Ireland / Shannon catchment.
    "eu-west-1": {
        "label": "Ireland",
        "co2_kg_per_kwh": 0.30,
        "water_l_per_kwh": 0.80,
        "wue_direct_l_per_kwh": 0.03,
        "wue_source": "aws_2024_ireland",
        "water_stress_score": 0.0,
        "water_stress_level": "low",
        "water_stress_basin": "Shannon",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # AWS has no Paris-region WUE in the 2024 table; temperate Western Europe estimate.
    # WRI Aqueduct 2023 BWS: medium; Seine basin.
    "eu-west-3": {
        "label": "France",
        "co2_kg_per_kwh": 0.06,
        "water_l_per_kwh": 2.50,
        "wue_direct_l_per_kwh": 0.10,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.5,
        "water_stress_level": "medium",
        "water_stress_basin": "Seine",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: low–medium; Lake Mälaren basin.
    "eu-north-1": {
        "label": "Sweden",
        "co2_kg_per_kwh": 0.04,
        "water_l_per_kwh": 5.00,
        "wue_direct_l_per_kwh": 0.02,
        "wue_source": "aws_2024_stockholm",
        "water_stress_score": 0.25,
        "water_stress_level": "low-medium",
        "water_stress_basin": "Lake Mälaren",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: medium; Main River basin.
    "eu-central-1": {
        "label": "Germany (Frankfurt)",
        "co2_kg_per_kwh": 0.38,
        "water_l_per_kwh": 1.20,
        "wue_direct_l_per_kwh": 0.01,
        "wue_source": "aws_2024_frankfurt",
        "water_stress_score": 0.5,
        "water_stress_level": "medium",
        "water_stress_basin": "Main",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: medium; Tone River basin.
    "ap-northeast-1": {
        "label": "Japan (Tokyo)",
        "co2_kg_per_kwh": 0.45,
        "water_l_per_kwh": 1.00,
        "wue_direct_l_per_kwh": 0.91,
        "wue_source": "aws_2024_tokyo",
        "water_stress_score": 0.5,
        "water_stress_level": "medium",
        "water_stress_basin": "Tone River",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # AWS Asia-Pacific (Mumbai) WUE N/A in 2024 report; hot-humid climate estimate.
    # WRI Aqueduct 2023 BWS: extremely high; Krishna basin (Western India).
    "ap-south-1": {
        "label": "India (Mumbai)",
        "co2_kg_per_kwh": 0.71,
        "water_l_per_kwh": 2.30,
        "wue_direct_l_per_kwh": 1.40,
        "wue_source": "climate_estimate",
        "water_stress_score": 1.0,
        "water_stress_level": "extremely-high",
        "water_stress_basin": "Krishna",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # WRI Aqueduct 2023 BWS: high; Singapore–Johor coastal basin.
    "ap-southeast-1": {
        "label": "Singapore",
        "co2_kg_per_kwh": 0.41,
        "water_l_per_kwh": 1.10,
        "wue_direct_l_per_kwh": 1.68,
        "wue_source": "aws_2024_singapore",
        "water_stress_score": 0.85,
        "water_stress_level": "high",
        "water_stress_basin": "Singapore-Johor",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # CO₂ 0.07 kg/kWh (IEA 2024, hydro-dominated Brazil grid).
    # Indirect water 9.0 L/kWh (high hydro share, NREL Macknick et al. 2012).
    # WUE 0.6 L/kWh IT (warm/humid climate estimate; AWS 2024 N/A for São Paulo).
    # WRI Aqueduct 2023 BWS: medium; Paraíba do Sul basin.
    "sa-east-1": {
        "label": "Brazil (São Paulo)",
        "co2_kg_per_kwh": 0.07,
        "water_l_per_kwh": 9.00,
        "wue_direct_l_per_kwh": 0.60,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.30,
        "water_stress_level": "medium",
        "water_stress_basin": "Paraíba do Sul",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # CO₂ 0.03 kg/kWh (IEA 2024, mostly hydro Quebec grid).
    # Indirect water 7.5 L/kWh (hydro reservoir evaporation, NREL Macknick et al. 2012).
    # WUE 0.15 L/kWh IT (cool climate estimate; AWS 2024 N/A for Montreal).
    # WRI Aqueduct 2023 BWS: low; St. Lawrence basin.
    "ca-central-1": {
        "label": "Canada (Montreal)",
        "co2_kg_per_kwh": 0.03,
        "water_l_per_kwh": 7.50,
        "wue_direct_l_per_kwh": 0.15,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.10,
        "water_stress_level": "low",
        "water_stress_basin": "St. Lawrence",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # CO₂ 0.65 kg/kWh (AEMO NEM 2023, coal-heavy NSW grid).
    # Indirect water 1.3 L/kWh (coal + gas thermoelectric, NREL Macknick et al. 2012).
    # WUE 0.45 L/kWh IT (warm/dry climate estimate; AWS 2024 N/A for Sydney).
    # WRI Aqueduct 2023 BWS: medium-high; Hawkesbury-Nepean basin.
    "ap-southeast-2": {
        "label": "Australia (Sydney)",
        "co2_kg_per_kwh": 0.65,
        "water_l_per_kwh": 1.30,
        "wue_direct_l_per_kwh": 0.45,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.40,
        "water_stress_level": "medium-high",
        "water_stress_basin": "Hawkesbury-Nepean",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # CO₂ 0.71 kg/kWh (IEA 2024, gas + coal Hong Kong grid).
    # Indirect water 1.2 L/kWh (thermoelectric mix, NREL Macknick et al. 2012).
    # WUE 0.7 L/kWh IT (tropical high-evaporative climate estimate; AWS 2024 N/A).
    # WRI Aqueduct 2023 BWS: high; Pearl River basin.
    "ap-east-1": {
        "label": "Hong Kong",
        "co2_kg_per_kwh": 0.71,
        "water_l_per_kwh": 1.20,
        "wue_direct_l_per_kwh": 0.70,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.55,
        "water_stress_level": "high",
        "water_stress_basin": "Pearl River",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # CO₂ 0.42 kg/kWh (IEA 2024, nuclear + coal Korea grid).
    # Indirect water 1.8 L/kWh (nuclear + coal cooling water, NREL Macknick et al. 2012).
    # WUE 0.4 L/kWh IT (temperate climate estimate; AWS 2024 N/A for Seoul).
    # WRI Aqueduct 2023 BWS: medium-high; Han River basin.
    "ap-northeast-2": {
        "label": "Korea (Seoul)",
        "co2_kg_per_kwh": 0.42,
        "water_l_per_kwh": 1.80,
        "wue_direct_l_per_kwh": 0.40,
        "wue_source": "climate_estimate",
        "water_stress_score": 0.45,
        "water_stress_level": "medium-high",
        "water_stress_basin": "Han River",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
    # Multi-basin global average; medium stress as conservative default.
    "global-avg": {
        "label": "Global average (IEA 2024)",
        "co2_kg_per_kwh": 0.48,
        "water_l_per_kwh": 1.80,
        "wue_direct_l_per_kwh": 0.15,
        "wue_source": "aws_2024_global",
        "water_stress_score": 0.5,
        "water_stress_level": "medium",
        "water_stress_basin": "Global multi-basin average",
        "stress_source": STRESS_SOURCE_DEFAULT,
    },
}

# ElectricityMaps API v0.2 — https://static.electricitymap.org/api/docs/
# Zone list (no auth): https://api.electricitymap.org/v3/zones; auth returns entitled zones only.
EM_API_BASE = "https://api.electricitymap.org/v3/carbon-intensity/latest"
EM_ZONES_API = "https://api.electricitymap.org/v3/zones"
CARBON_SOURCE_STATIC = "static_avg"
CARBON_SOURCE_EM = "em_realtime"

# Watermark region → Electricity Maps zoneKey (validated against /v3/zones).
EM_ZONE_MAP = {
    "us-east-1":      "US-MIDA-PJM",   # PJM Mid-Atlantic (Virginia)
    "us-east-2":      "US-MIDW-MISO",  # Midcontinent ISO (Ohio)
    "us-west-1":      "US-CAL-CISO",   # California ISO
    "us-west-2":      "US-NW-PACW",    # PacifiCorp West (Oregon)
    "eu-west-1":      "IE",            # Ireland
    "eu-west-3":      "FR",            # France
    "eu-north-1":     "SE",            # Sweden
    "eu-central-1":   "DE",            # Germany (Frankfurt)
    "ap-northeast-1": "JP-TK",         # Japan — Tōkyō
    "ap-south-1":     "IN-WE",         # India — Western (Mumbai)
    "ap-southeast-1": "SG",            # Singapore
    "sa-east-1":      "BR-CS",         # Brazil — Central (São Paulo; EM has no BR-SP)
    "ca-central-1":   "CA-QC",         # Canada — Québec (Montreal)
    "ap-southeast-2": "AU-NSW",        # Australia — New South Wales (Sydney)
    "ap-east-1":      "HK",            # Hong Kong
    "ap-northeast-2": "KR",            # South Korea (Seoul)
}

_GRID_PROFILE_CACHE: dict[tuple, dict] = {}
_WATER_PROFILE_CACHE: dict[tuple, dict] = {}

# Impact dimension registry — documents energy basis for each output (v0.3+ extensible).
IMPACT_DIMENSIONS = {
    "operational_energy": {"unit": "kwh", "basis": "facility"},
    "operational_carbon": {"unit": "kgco2e", "basis": "facility"},
    "operational_water_direct": {"unit": "l", "basis": "it"},
    "operational_water_indirect": {"unit": "l", "basis": "facility"},
    "embodied_carbon": {"unit": "kgco2e", "basis": "hardware_amortized"},
    "embodied_water": {"unit": "l", "basis": "hardware_amortized"},
}

# Default useful life for amortization (5 years × 8760 h).
GPU_USEFUL_LIFE_HOURS = 43800
SERVER_USEFUL_LIFE_HOURS = 87600

# Embodied (cradle-to-gate) manufacturing impacts per hardware SKU.
# Amortized linearly over useful_life_hours for each run.
HARDWARE_PROFILES = {
    # NVIDIA HGX H100 PCF Summary (ISO 14067, third-party reviewed by WSP, FY25):
    #   1,312 kg CO2e cradle-to-gate for the 8-GPU HGX H100 baseboard.
    #   Per-GPU allocation: 1312 / 8 = 164 kg CO2e (compute-only SXM module share).
    #   https://images.nvidia.com/aem-dam/Solutions/documents/HGX-H100-PCF-Summary.pdf
    # Water: no NVIDIA chip-level freshwater PCF; TSMC advanced-fab intensity proxy
    #   (Wang et al. 2023 Water Cycle 4:47-54; scaled for 5nm vs A100 7nm).
    "h100-sxm": {
        "label": "NVIDIA H100 SXM",
        "embodied_co2e_kg": 164.0,
        "embodied_water_l": 2800.0,
        "useful_life_hours": GPU_USEFUL_LIFE_HOURS,
        "citation": "NVIDIA HGX H100 PCF Summary 2025 (ISO 14067); water TSMC fab proxy",
    },
    # Bouzar et al., "More than Carbon" (arXiv:2509.00093) — primary-data cradle-to-gate
    #   for a single NVIDIA A100 SXM 40GB GPU: 127.6 kg CO2e.
    # Water: manufacturing freshwater midpoint from same study; TSMC fab where unavailable.
    "a100": {
        "label": "NVIDIA A100 SXM 40GB",
        "embodied_co2e_kg": 127.6,
        "embodied_water_l": 2200.0,
        "useful_life_hours": GPU_USEFUL_LIFE_HOURS,
        "citation": "Bouzar et al. arXiv:2509.00093 cradle-to-gate A100 LCA",
    },
    # V100: no chip-level freshwater PCF in vendor docs; ICT manufacturing midpoint from
    #   Gupta et al. "Chasing Carbon" (2022) scaled to Volta-era fab intensity.
    "v100": {
        "label": "NVIDIA V100 SXM",
        "embodied_co2e_kg": 72.0,
        "embodied_water_l": 1800.0,
        "useful_life_hours": GPU_USEFUL_LIFE_HOURS,
        "citation": "Gupta et al. 2022 ICT hardware manufacturing water proxy (Volta-class)",
    },
    # Dell PowerEdge-class 2U rack server — cradle-to-gate manufacturing share.
    #   Dell PowerEdge R640 PAIA PCF ~7,730 kg CO2e lifetime (Jan 2019); manufacturing
    #   ~10-12% of total ≈ 850 kg CO2e cradle-to-gate for CPU/RAM/mainboard assembly.
    #   https://i.dell.com/sites/csdocuments/corpcomm_docs/en/carbon-footprint-poweredge-r640.pdf
    # Water: enterprise server manufacturing proxy (HP/Dell LCA databases; fab + assembly).
    "generic-2u": {
        "label": "Generic 2U rack server",
        "embodied_co2e_kg": 850.0,
        "embodied_water_l": 12000.0,
        "useful_life_hours": SERVER_USEFUL_LIFE_HOURS,
        "citation": "Dell PowerEdge R640 PAIA PCF manufacturing-stage allocation",
    },
    # Conservative GPU average when SKU unknown but embodied requested explicitly.
    #   Luccioni et al. FAccT 2023 uses ~150 kg CO2e/card for A100-class accelerators.
    "default-gpu": {
        "label": "Generic GPU accelerator (conservative average)",
        "embodied_co2e_kg": 150.0,
        "embodied_water_l": 2000.0,
        "useful_life_hours": GPU_USEFUL_LIFE_HOURS,
        "citation": "Luccioni et al. FAccT 2023 embodied GPU estimate; ICT hardware LCA average",
    },
}

# Substrings matched against --hardware / --workload-name for auto SKU selection.
_HARDWARE_SKU_ALIASES: dict[str, tuple[str, ...]] = {
    "h100-sxm": ("h100 sxm", "h100-sxm", "hgx h100", "h100 80gb", "h100"),
    "a100": ("a100 sxm", "a100 80gb", "a100 40gb", "a100"),
    "v100": ("v100 sxm", "v100 32gb", "v100"),
    "generic-2u": ("generic-2u", "generic 2u", "poweredge", "proliant", "2u server", "rack server"),
}


def resolve_hardware_sku(
    hardware_sku: str | None,
    hardware_label: str | None = None,
) -> str | None:
    """Resolve SKU from --hardware-sku or recognizable --hardware text."""
    if hardware_sku:
        canonical = _normalize_hardware_sku(hardware_sku)
        if canonical in HARDWARE_PROFILES:
            return canonical
        return None
    if not hardware_label:
        return None
    label = hardware_label.lower()
    for sku, tokens in _HARDWARE_SKU_ALIASES.items():
        if any(token in label for token in tokens):
            return sku
    return None


def _normalize_hardware_sku(sku: str) -> str:
    """Map CLI aliases (e.g. generic) to profile keys."""
    key = sku.strip().lower()
    if key == "generic":
        return "generic-2u"
    return key


def compute_embodied_impacts(
    duration_s: float,
    hardware_sku: str | None,
    requested_sku: str | None = None,
) -> dict:
    """
    Amortize cradle-to-gate manufacturing CO2e and water over useful life.

    embodied_for_run = embodied_total * (duration_s / 3600) / useful_life_hours
    """
    sku_display = hardware_sku or requested_sku
    if not hardware_sku or hardware_sku not in HARDWARE_PROFILES:
        return {
            "co2e_kg": 0.0,
            "water_l": 0.0,
            "embodied_water_ml": None,
            "sku": sku_display,
            "useful_life_hours": None,
            "source": "no_profile",
            "citation": None,
            "label": None,
        }

    profile = HARDWARE_PROFILES[hardware_sku]
    duration_h = max(duration_s, 0.0) / 3600.0
    life_h = profile["useful_life_hours"]
    share = duration_h / life_h if life_h > 0 else 0.0
    water_l = round(profile["embodied_water_l"] * share, 8)
    return {
        "co2e_kg": round(profile["embodied_co2e_kg"] * share, 8),
        "water_l": water_l,
        "embodied_water_ml": round(water_l * 1000, 4),
        "sku": hardware_sku,
        "useful_life_hours": life_h,
        "source": "modeled",
        "citation": profile["citation"],
        "label": profile["label"],
        "embodied_co2e_kg_total": profile["embodied_co2e_kg"],
        "embodied_water_l_total": profile["embodied_water_l"],
    }


def static_grid_profile(region: str) -> dict:
    """Annual-average regional grid profile (modeled)."""
    if region not in REGION_PROFILES:
        raise ValueError(f"Unknown region: {region}")
    profile = REGION_PROFILES[region].copy()
    profile["carbon_source"] = CARBON_SOURCE_STATIC
    profile["water_source"] = CARBON_SOURCE_STATIC
    profile["source"] = CARBON_SOURCE_STATIC
    profile["timestamp"] = None
    profile["em_zone"] = None
    return profile


def _grid_warn(message: str) -> None:
    print(f"[watermark] warning: {message}", file=sys.stderr)


def _warn_em_key_if_static(grid_source: str) -> None:
    """Warn when an API key is present but grid source was not explicitly opted in."""
    if grid_source != "static":
        return
    if os.environ.get("ELECTRICITYMAPS_API_KEY", "").strip():
        _grid_warn(
            "ELECTRICITYMAPS_API_KEY detected; pass --grid-source electricitymaps "
            "to use realtime carbon"
        )


def _warn_operator_disclosure_if_static(region: str, water_source: str) -> None:
    if water_source != "static":
        return
    from operator_disclosures import regions_with_disclosures
    if region in regions_with_disclosures():
        _grid_warn(
            f"Operator disclosure available for {region}; pass --water-source operator to use it"
        )


def fetch_em_zone_keys() -> set[str]:
    """Return all valid Electricity Maps zoneKey values from /v3/zones (no auth)."""
    req = urllib.request.Request(EM_ZONES_API, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        return set(payload.keys())
    raise ValueError("unexpected /v3/zones response shape")


def fetch_em_entitled_zone_keys(api_key: str) -> set[str]:
    """Return zone keys the API key is entitled to access (auth required)."""
    req = urllib.request.Request(
        EM_ZONES_API,
        headers={"auth-token": api_key, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        return set(payload.keys())
    raise ValueError("unexpected authenticated /v3/zones response shape")


def invalid_em_zone_map_entries(valid_zones: set[str] | None = None) -> list[tuple[str, str]]:
    """Return (region, zoneKey) pairs not present in the live zone list."""
    zones = valid_zones if valid_zones is not None else fetch_em_zone_keys()
    return [(region, zone) for region, zone in EM_ZONE_MAP.items() if zone not in zones]


def _format_em_datetime(at: dt.datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _em_http_error_detail(exc: urllib.error.HTTPError) -> str:
    body = ""
    if exc.fp is not None:
        try:
            body = exc.fp.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    if body:
        return f"HTTP {exc.code}: {body}"
    return f"HTTP {exc.code}: {exc.reason}"


def _em_request(api_key: str, params: dict) -> dict:
    url = f"{EM_API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"auth-token": api_key, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_em_carbon_response(payload: dict) -> tuple[float, str | None, str | None]:
    """Parse EM response; carbonIntensity is gCO2eq/kWh — convert to kg/kWh."""
    intensity = payload.get("carbonIntensity")
    if isinstance(intensity, (int, float)):
        return intensity / 1000.0, payload.get("datetime"), payload.get("zone")

    data = payload.get("data")
    if isinstance(data, list) and data:
        entry = data[0]
        intensity = entry.get("carbonIntensity")
        if isinstance(intensity, (int, float)):
            return (
                intensity / 1000.0,
                entry.get("datetime") or payload.get("datetime"),
                entry.get("zone") or payload.get("zone"),
            )

    raise ValueError("ElectricityMaps response missing carbonIntensity")


def _electricitymaps_entitlement_hint(api_key: str, zone_key: str) -> str:
    try:
        entitled = fetch_em_entitled_zone_keys(api_key)
    except Exception:
        return ""
    if not entitled:
        return ""
    if zone_key in entitled:
        return ""
    preview = ", ".join(sorted(entitled)[:5])
    suffix = "..." if len(entitled) > 5 else ""
    return (
        f" API key is entitled to zone(s): {preview}{suffix}. "
        f"Free-tier keys are limited to one zone — set it in the Electricity Maps portal "
        f"to {zone_key} or upgrade your plan."
    )


def _electricitymaps_carbon_at(api_key: str, region: str, at: dt.datetime) -> tuple[float, str | None, str | None]:
    """
    Fetch latest carbon intensity for a watermark region via Electricity Maps v3 zone API.
    """
    _ = at  # run start time recorded in profile cache key; /latest needs zone only
    em_zone = EM_ZONE_MAP.get(region)
    if not em_zone:
        raise RuntimeError(f"no ElectricityMaps zone mapping for region {region}")

    params = {"zone": em_zone}
    try:
        payload = _em_request(api_key, params)
        co2, ts, zone = _parse_em_carbon_response(payload)
        return co2, ts, zone or em_zone
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        last_error = exc

    hint = ""
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 403:
        hint = _electricitymaps_entitlement_hint(api_key, em_zone)

    detail = _em_http_error_detail(last_error) if isinstance(last_error, urllib.error.HTTPError) else str(last_error)
    raise RuntimeError(
        f"ElectricityMaps lookup failed for {region} (zone {em_zone}): {detail}.{hint}"
    )


def _fetch_grid_profile_electricitymaps(region: str, at: dt.datetime) -> dict:
    """Fetch real-time carbon from ElectricityMaps; fall back to static on any failure."""
    profile = static_grid_profile(region)

    if region == "global-avg":
        _grid_warn("ElectricityMaps has no data-center mapping for global-avg; using static_avg carbon")
        return profile

    api_key = os.environ.get("ELECTRICITYMAPS_API_KEY", "").strip()
    if not api_key:
        _grid_warn("ELECTRICITYMAPS_API_KEY not set; falling back to static_avg carbon")
        return profile

    try:
        co2_kg_per_kwh, timestamp, em_zone = _electricitymaps_carbon_at(api_key, region, at)
        profile["co2_kg_per_kwh"] = co2_kg_per_kwh
        profile["carbon_source"] = CARBON_SOURCE_EM
        profile["source"] = CARBON_SOURCE_EM
        profile["timestamp"] = timestamp
        profile["em_zone"] = em_zone
    except Exception as exc:
        _grid_warn(f"ElectricityMaps API failed ({exc}); falling back to static_avg carbon")

    return profile


def fetch_grid_profile(region: str, at: dt.datetime | None = None,
                       grid_source: str = "static") -> dict:
    """
    Load grid profile for carbon/water modeling. One API call per run when
    grid_source is 'electricitymaps'; result is cached for the run.
    """
    if region not in REGION_PROFILES:
        raise ValueError(f"Unknown region: {region}")

    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    cache_key = (grid_source, region, _format_em_datetime(at))
    if cache_key in _GRID_PROFILE_CACHE:
        return _GRID_PROFILE_CACHE[cache_key].copy()

    if grid_source == "static":
        profile = static_grid_profile(region)
    elif grid_source == "electricitymaps":
        profile = _fetch_grid_profile_electricitymaps(region, at)
    else:
        raise ValueError(f"Unknown grid_source: {grid_source}")

    _GRID_PROFILE_CACHE[cache_key] = profile
    return profile.copy()


def normalize_cooling_type(raw: str | None) -> str:
    """Map CLI/metadata cooling labels to canonical cooling_type enum."""
    if not raw or not str(raw).strip():
        return "unknown"
    key = str(raw).strip().lower().replace("_", "-")
    aliases = {
        "evaporative": "evaporative",
        "evap": "evaporative",
        "air": "air",
        "air-cooled": "air",
        "dry": "dry",
        "dry-cooler": "dry",
        "dry-cooling": "dry",
        "liquid": "liquid",
        "liquid-cooling": "liquid",
        "chilled-water": "liquid",
        "chilled": "liquid",
        "closed-loop": "liquid",
        "immersion": "immersion",
        "direct-to-chip": "liquid",
    }
    return aliases.get(key, "unknown")


def season_from_datetime(at: dt.datetime) -> str:
    """Northern-hemisphere meteorological season from UTC timestamp."""
    month = at.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "fall"


def _seasonal_stress_multiplier(region: str, season: str) -> float:
    multipliers = dict(DEFAULT_SEASONAL_STRESS_MULTIPLIERS)
    multipliers.update(REGION_SEASONAL_STRESS_MULTIPLIERS.get(region, {}))
    return multipliers.get(season, 1.0)


def static_water_profile(
    region: str,
    at: dt.datetime,
    *,
    cooling_type: str = "unknown",
    wue_direct_override: float | None = None,
    water_stress_season: str | None = None,
) -> dict:
    """Regional water profile: WUE, indirect intensity, seasonal Aqueduct stress."""
    if region not in REGION_PROFILES:
        raise ValueError(f"Unknown region: {region}")
    base = REGION_PROFILES[region]
    cooling = normalize_cooling_type(cooling_type)
    season = water_stress_season or "annual"
    if season not in DEFAULT_SEASONAL_STRESS_MULTIPLIERS:
        season = "annual"

    regional_wue = (
        wue_direct_override
        if wue_direct_override is not None
        else base["wue_direct_l_per_kwh"]
    )
    wue = regional_wue * COOLING_WUE_MULTIPLIERS.get(cooling, 1.0)
    base_stress = float(base.get("water_stress_score", 0.0))
    stress_score = min(1.0, base_stress * _seasonal_stress_multiplier(region, season))

    return {
        "wue_direct_l_per_kwh": wue,
        "wue_regional_base_l_per_kwh": regional_wue,
        "wue_source": base.get("wue_source", "region_default"),
        "water_l_per_kwh": base["water_l_per_kwh"],
        "water_stress_score": stress_score,
        "water_stress_level": base.get("water_stress_level"),
        "water_stress_basin": base.get("water_stress_basin"),
        "stress_source": base.get("stress_source", STRESS_SOURCE_DEFAULT),
        "water_stress_season": season,
        "water_stress_as_of": _format_em_datetime(at),
        "accounting_method": WATER_ACCOUNTING_CONSUMPTION,
        "water_source": WATER_SOURCE_STATIC,
        "cooling_type": cooling,
    }


def operator_water_profile(
    region: str,
    at: dt.datetime,
    *,
    cooling_type: str = "unknown",
    wue_direct_override: float | None = None,
    water_stress_season: str | None = None,
) -> dict:
    from operator_disclosures import get_operator_disclosure

    disclosure = get_operator_disclosure(region)
    if disclosure is None:
        raise ValueError(f"No operator disclosure for region: {region}")

    base = static_water_profile(
        region,
        at,
        cooling_type=cooling_type,
        wue_direct_override=wue_direct_override,
        water_stress_season=water_stress_season,
    )
    regional_wue = disclosure["wue_l_per_kwh"]
    cooling = normalize_cooling_type(cooling_type)
    wue = (
        wue_direct_override
        if wue_direct_override is not None
        else regional_wue * COOLING_WUE_MULTIPLIERS.get(cooling, 1.0)
    )
    return {
        **base,
        "wue_direct_l_per_kwh": wue,
        "wue_regional_base_l_per_kwh": regional_wue,
        "wue_source": disclosure["disclosure_source"],
        "accounting_method": disclosure["accounting_method"],
        "water_source": WATER_SOURCE_OPERATOR,
        "operator": disclosure["operator"],
        "operator_disclosure_source": disclosure["disclosure_source"],
        "disclosure_type": disclosure["disclosure_type"],
        "disclosure_verified": disclosure["disclosure_verified"],
        "disclosure_year": disclosure["disclosure_year"],
        "disclosure_notes": disclosure.get("notes"),
        "cooling_type": cooling,
    }


def fetch_water_profile(
    region: str,
    at: dt.datetime | None = None,
    *,
    water_source: str = "static",
    cooling_type: str = "unknown",
    wue_direct_override: float | None = None,
    water_stress_season: str | None = None,
) -> dict:
    """
    Load water profile for direct/indirect modeling.

    static: regional WUE + Aqueduct stress tables (default, offline)
    operator: facility-reported WUE from disclosure registry
    """
    if region not in REGION_PROFILES:
        raise ValueError(f"Unknown region: {region}")

    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    cache_key = (
        water_source,
        region,
        _format_em_datetime(at),
        normalize_cooling_type(cooling_type),
        wue_direct_override,
        water_stress_season or "",
    )
    if cache_key in _WATER_PROFILE_CACHE:
        return _WATER_PROFILE_CACHE[cache_key].copy()

    if water_source == "static":
        profile = static_water_profile(
            region,
            at,
            cooling_type=cooling_type,
            wue_direct_override=wue_direct_override,
            water_stress_season=water_stress_season,
        )
    elif water_source == "operator":
        profile = operator_water_profile(
            region,
            at,
            cooling_type=cooling_type,
            wue_direct_override=wue_direct_override,
            water_stress_season=water_stress_season,
        )
    else:
        raise ValueError(f"Unknown water_source: {water_source}")

    _WATER_PROFILE_CACHE[cache_key] = profile
    return profile.copy()


def stress_badge_label(stress_level: str | None) -> str:
    """Human-readable badge for dashboard regional cards."""
    labels = {
        "low": "LOW STRESS",
        "low-medium": "LOW–MEDIUM STRESS",
        "medium": "MEDIUM STRESS",
        "medium-high": "MEDIUM–HIGH STRESS",
        "high": "HIGH STRESS",
        "extremely-high": "EXTREME STRESS",
    }
    if not stress_level:
        return "UNKNOWN STRESS"
    return labels.get(stress_level, stress_level.upper().replace("-", " ") + " STRESS")


def apply_water_stress_weighting(water: dict, water_profile: dict) -> dict:
    """
    Apply modest watershed-stress multiplier to gross water total.

    WWL (Watershed-Weighted Liters) = total_l × (1 + stress_score)
    stress_score maps 0.0 (low) … 1.0 (extremely high) from WRI Aqueduct BWS.
    """
    stress_score = water_profile.get("water_stress_score", 0.0)
    total_l = water["total_l"]
    wwl_l = total_l * (1 + stress_score)
    return {
        **water,
        "stress_score": stress_score,
        "stress_level": water_profile.get("water_stress_level"),
        "stress_basin": water_profile.get("water_stress_basin"),
        "stress_source": water_profile.get("stress_source", STRESS_SOURCE_DEFAULT),
        "stress_weighted_total_l": round(wwl_l, 6),
        "wwl_l": round(wwl_l, 6),
        "wwl_ml": round(wwl_l * 1000, 4),
        "weighting_methodology": WATER_STRESS_WEIGHTING_METHOD,
        "water_accounting_method": water_profile.get(
            "accounting_method", WATER_ACCOUNTING_UNKNOWN,
        ),
        "water_source": water_profile.get("water_source", WATER_SOURCE_STATIC),
    }


def compute_impacts(
    it_kwh: float,
    facility_kwh: float,
    grid_profile: dict,
    water_profile: dict,
    *,
    wue_direct: float | None = None,
    wwl_per_unit: float | None = None,
) -> dict:
    """Pure aggregation of IT/facility energy into carbon and water impacts."""
    carbon_source = grid_profile.get("carbon_source", CARBON_SOURCE_STATIC)
    wue = wue_direct if wue_direct is not None else water_profile["wue_direct_l_per_kwh"]
    water_direct_l = it_kwh * wue
    water_indirect_l = facility_kwh * water_profile["water_l_per_kwh"]
    water = apply_water_stress_weighting({
        "direct_cooling_l": water_direct_l,
        "indirect_generation_l": water_indirect_l,
        "total_l": water_direct_l + water_indirect_l,
        "direct_source": "modeled_wue",
        "indirect_source": water_profile.get("water_source", WATER_SOURCE_STATIC),
    }, water_profile)
    if wwl_per_unit is not None:
        water["wwl_per_unit_ml"] = round(wwl_per_unit * 1000, 4)
    return {
        "energy": {
            "it_total_kwh": it_kwh,
            "facility_total_kwh": facility_kwh,
            "facility_source": "pue_multiplier",
        },
        "carbon": {
            "co2e_kg": facility_kwh * grid_profile["co2_kg_per_kwh"],
            "source": carbon_source,
            "energy_basis_kwh": facility_kwh,
        },
        "water": water,
    }


def _parse_nvidia_smi_power(stdout: str) -> float | None:
    """Parse nvidia-smi power.draw output, skipping [N/A] and malformed lines."""
    total = 0.0
    valid = 0
    for line in stdout.strip().splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token or token.startswith("["):
            continue
        try:
            total += float(token)
            valid += 1
        except ValueError:
            continue
    return total if valid else None


# ---------------------------------------------------------------------------
# Hardware probes
# ---------------------------------------------------------------------------

@runtime_checkable
class EnergyProbe(Protocol):
    """Minimal interface for v0.3 per-process attribution probes."""
    name: str
    available: bool

    def read_watts(self, interval_s: float) -> float | None: ...
    def shutdown(self) -> None: ...


class RAPLProbe:
    """
    Reads CPU/package energy from the Linux RAPL sysfs interface.
    Paths:
      Intel: /sys/class/powercap/intel-rapl:N/energy_uj
      AMD:   /sys/class/powercap/amd-rapl:N/energy_uj   (kernel 5.11+)

    Returns watts averaged over the interval since the previous read.
    Returns None if RAPL is not available (macOS, Windows, most cloud VMs).
    """

    name = "rapl"
    _RAPL_GLOBS = ("intel-rapl:*", "amd-rapl:*")
    _PACKAGE_NAMES = frozenset({"package", "package-0", "package-1"})

    def __init__(self):
        self.available = False
        self.zones: list[dict] = []
        self.platform: str | None = None
        if platform.system() != "Linux":
            return
        rapl_root = Path("/sys/class/powercap")
        if not rapl_root.exists():
            return
        for pattern in self._RAPL_GLOBS:
            for zone in sorted(rapl_root.glob(pattern)):
                if zone.name.count(":") != 1:
                    continue
                energy_file = zone / "energy_uj"
                max_file = zone / "max_energy_range_uj"
                name_file = zone / "name"
                if not energy_file.exists() or not os.access(energy_file, os.R_OK):
                    continue
                try:
                    zone_name = name_file.read_text().strip() if name_file.exists() else ""
                    if zone_name and zone_name.lower() not in self._PACKAGE_NAMES:
                        if not zone_name.lower().startswith("package"):
                            continue
                    max_uj = int(max_file.read_text().strip()) if max_file.exists() else None
                    self.zones.append({"path": energy_file, "max_uj": max_uj, "last_uj": None})
                    if self.platform is None:
                        self.platform = "amd" if pattern.startswith("amd") else "intel"
                except (OSError, ValueError):
                    continue
        self.available = len(self.zones) > 0

    def read_watts(self, interval_s: float) -> float | None:
        if not self.available or interval_s <= 0:
            return None
        total_delta_uj = 0.0
        zones_read = 0
        for zone in self.zones:
            try:
                current = int(zone["path"].read_text().strip())
            except (OSError, ValueError):
                continue
            zones_read += 1
            if zone["last_uj"] is not None:
                delta = current - zone["last_uj"]
                if delta < 0:
                    if zone["max_uj"]:
                        delta += zone["max_uj"]
                    else:
                        zone["last_uj"] = current
                        return None
                total_delta_uj += max(delta, 0)
            zone["last_uj"] = current
        if zones_read == 0:
            return None
        if total_delta_uj == 0 and all(z["last_uj"] is not None for z in self.zones):
            return 0.0
        if all(z["last_uj"] is None for z in self.zones):
            return None
        return (total_delta_uj / 1_000_000.0) / interval_s

    def shutdown(self) -> None:
        pass


class NVMLProbe:
    """
    Reads GPU power via pynvml when available; falls back to invoking nvidia-smi.
    Returns total watts across all visible NVIDIA GPUs, or None if no GPU.
    """

    name = "nvml"

    def __init__(self):
        self.available = False
        self.mode: str | None = None
        self.handles: list = []
        self.pynvml = None
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                self.pynvml = pynvml
                self.handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
                self.available = True
                self.mode = "nvml_pynvml"
                return
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and _parse_nvidia_smi_power(result.stdout) is not None:
                self.available = True
                self.mode = "nvml_smi"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def read_watts(self, interval_s: float = 0.0) -> float | None:
        _ = interval_s
        if not self.available:
            return None
        if self.mode == "nvml_pynvml":
            try:
                return sum(self.pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0 for h in self.handles)
            except Exception:
                return None
        if self.mode == "nvml_smi":
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2,
                )
                if result.returncode != 0:
                    return None
                return _parse_nvidia_smi_power(result.stdout)
            except Exception:
                return None
        return None

    def shutdown(self) -> None:
        if self.pynvml:
            try:
                self.pynvml.nvmlShutdown()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Modeled fallback for CPU when RAPL is unavailable
# ---------------------------------------------------------------------------

def model_cpu_watts_from_util(cpu_percent: float, cpu_tdp_w: float = 65.0,
                              idle_fraction: float = 0.30) -> float:
    """
    Approximate CPU power when no hardware counter is available.
    Model:  idle_draw + (TDP - idle_draw) * (utilization)
    This is acknowledged in the summary as 'modeled_from_util' so a reviewer
    can see which numbers come from hardware and which come from a model.
    """
    idle_w = cpu_tdp_w * idle_fraction
    return idle_w + (cpu_tdp_w - idle_w) * max(0.0, min(1.0, cpu_percent / 100.0))


# ---------------------------------------------------------------------------
# Workload typing and per-unit normalization
# ---------------------------------------------------------------------------

def infer_workload_type(
    *,
    training_steps: int | None = None,
    token_count: int | None = None,
    request_count: int | None = None,
    image_count: int | None = None,
    workload_name: str | None = None,
    explicit: str | None = None,
) -> str:
    if explicit in ("training", "inference", "benchmark", "unknown"):
        return explicit
    text = (workload_name or "").lower()
    if training_steps or "train" in text:
        return "training"
    if token_count or request_count or image_count:
        return "inference"
    if "benchmark" in text:
        return "benchmark"
    return "unknown"


def build_per_unit_block(
    impacts: dict,
    *,
    unit_type: str | None,
    unit_count: int | None,
    facility_wh: float,
    normalization_source: str | None = None,
) -> dict | None:
    if not unit_type or not unit_count or unit_count <= 0:
        return None
    wwl_ml = impacts["water"]["wwl_ml"]
    carbon_g = impacts["carbon"]["co2e_kg"] * 1000
    block = {
        "unit_type": unit_type,
        "unit_count": unit_count,
        "wwl_ml_per_unit": round(wwl_ml / unit_count, 6),
        "energy_wh_per_unit": round(facility_wh / unit_count, 6),
        "carbon_g_per_unit": round(carbon_g / unit_count, 6),
    }
    if normalization_source:
        block["normalization_source"] = normalization_source
    return block


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------

class Meter:
    def __init__(self, region, pue, wue_direct, interval_s, cpu_tdp_fallback, output_dir,
                 scope="host", scope_pid=None, pue_source="default_iea_2024",
                 wue_source=None, grid_source="static", water_source="static",
                 cooling_type: str | None = None,
                 hardware_sku: str | None = None, hardware_sku_requested: str | None = None,
                 image_count: int | None = None,
                 token_count: int | None = None,
                 request_count: int | None = None,
                 training_steps: int | None = None,
                 workload_type: str | None = None,
                 water_stress_season: str | None = None):
        if scope != "host":
            raise NotImplementedError("per-process attribution is v0.3 (eBPF/Kepler)")
        if scope_pid is not None:
            raise NotImplementedError("per-process attribution is v0.3 (eBPF/Kepler)")

        self.region = region
        self.grid_source = grid_source
        self.water_source = water_source
        self.cooling_type = normalize_cooling_type(cooling_type)
        self.image_count = image_count
        self.token_count = token_count
        self.request_count = request_count
        self.training_steps = training_steps
        self.workload_type = workload_type
        self.water_stress_season = water_stress_season
        self.normalization_source: str | None = None
        self.run_at = dt.datetime.now(dt.timezone.utc)
        self.profile = fetch_grid_profile(
            region,
            at=self.run_at,
            grid_source=grid_source,
        )
        wue_override = wue_direct
        self.water_profile = fetch_water_profile(
            region,
            at=self.run_at,
            water_source=water_source,
            cooling_type=self.cooling_type,
            wue_direct_override=wue_override,
            water_stress_season=self.water_stress_season,
        )
        self.pue = pue
        self.pue_source = pue_source
        if wue_direct is None:
            self.wue_direct = self.water_profile["wue_direct_l_per_kwh"]
            self.wue_source = wue_source or self.water_profile.get("wue_source", "region_default")
        else:
            self.wue_direct = wue_direct
            self.wue_source = wue_source or "user_override"
        self.interval_s = interval_s
        self.cpu_tdp_fallback = cpu_tdp_fallback
        self.hardware_sku = hardware_sku
        self.hardware_sku_requested = hardware_sku_requested
        self.scope = scope
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.rapl = RAPLProbe()
        self.nvml = NVMLProbe()
        self.rapl_available_at_start = self.rapl.available
        if self.rapl.available:
            self.rapl.read_watts(self.interval_s)

        self.samples: list[dict] = []
        self._stop = threading.Event()

    def _sample_loop(self):
        try:
            import psutil  # type: ignore
        except ImportError:
            psutil = None

        if psutil:
            psutil.cpu_percent(interval=None)

        last_tick = time.monotonic()
        while not self._stop.is_set():
            time.sleep(self.interval_s)
            elapsed = time.monotonic() - last_tick
            last_tick = time.monotonic()
            ts = dt.datetime.now(dt.timezone.utc).isoformat()

            cpu_watts = self.rapl.read_watts(elapsed) if self.rapl.available else None
            cpu_source = "rapl_measured"
            if cpu_watts is None:
                util = psutil.cpu_percent(interval=None) if psutil else 0.0
                cpu_watts = model_cpu_watts_from_util(util, self.cpu_tdp_fallback)
                cpu_source = "modeled_from_util"

            gpu_watts = self.nvml.read_watts(elapsed) if self.nvml.available else None
            gpu_source = (self.nvml.mode if self.nvml.available else "no_gpu")

            mem_pct = psutil.virtual_memory().percent if psutil else None
            cpu_pct = psutil.cpu_percent(interval=None) if psutil else None

            self.samples.append({
                "timestamp": ts,
                "interval_s": self.interval_s,
                "elapsed_s": round(elapsed, 3),
                "cpu_watts": round(cpu_watts, 3) if cpu_watts is not None else None,
                "cpu_source": cpu_source,
                "gpu_watts": round(gpu_watts, 3) if gpu_watts is not None else None,
                "gpu_source": gpu_source,
                "cpu_percent": cpu_pct,
                "memory_percent": mem_pct,
            })

    def run_for(self, duration_s: float):
        if duration_s <= 0:
            raise ValueError(f"duration_s must be > 0, got {duration_s}")
        thread = threading.Thread(target=self._sample_loop, daemon=True)
        thread.start()
        time.sleep(duration_s)
        self._stop.set()
        thread.join(timeout=self.interval_s * 2)

    def run_command(self, cmd_argv):
        thread = threading.Thread(target=self._sample_loop, daemon=True)
        thread.start()
        try:
            proc = subprocess.Popen(cmd_argv)
            rc = proc.wait()
        except OSError as exc:
            self._stop.set()
            thread.join(timeout=self.interval_s * 2)
            raise RuntimeError(f"failed to start command: {exc}") from exc
        self._stop.set()
        thread.join(timeout=self.interval_s * 2)
        return rc

    def aggregate(self):
        if not self.samples:
            raise RuntimeError(
                "no samples collected; increase --duration or check that the workload ran long enough"
            )

        total_cpu_wh = 0.0
        total_gpu_wh = 0.0
        for s in self.samples:
            dt_s = s.get("elapsed_s", s["interval_s"])
            if s["cpu_watts"] is not None:
                total_cpu_wh += s["cpu_watts"] * (dt_s / 3600.0)
            if s["gpu_watts"] is not None:
                total_gpu_wh += s["gpu_watts"] * (dt_s / 3600.0)

        it_kwh = (total_cpu_wh + total_gpu_wh) / 1000.0
        facility_kwh = it_kwh * self.pue
        facility_wh = facility_kwh * 1000
        impacts = compute_impacts(
            it_kwh,
            facility_kwh,
            self.profile,
            self.water_profile,
            wue_direct=self.wue_direct,
        )

        unit_type = None
        unit_count = None
        if self.training_steps and self.training_steps > 0:
            unit_type, unit_count = "training_step", self.training_steps
        elif self.token_count and self.token_count > 0:
            unit_type, unit_count = "token", self.token_count
        elif self.request_count and self.request_count > 0:
            unit_type, unit_count = "request", self.request_count
        elif self.image_count and self.image_count > 0:
            unit_type, unit_count = "image", self.image_count

        per_unit = build_per_unit_block(
            impacts,
            unit_type=unit_type,
            unit_count=unit_count,
            facility_wh=facility_wh,
            normalization_source=self.normalization_source,
        )
        if per_unit:
            impacts["water"]["wwl_per_unit_ml"] = per_unit["wwl_ml_per_unit"]

        duration_s = round(sum(s.get("elapsed_s", s["interval_s"]) for s in self.samples), 3)
        embodied = compute_embodied_impacts(
            duration_s, self.hardware_sku, self.hardware_sku_requested,
        )

        cpu_sources = sorted({s["cpu_source"] for s in self.samples})
        gpu_sources = sorted({s["gpu_source"] for s in self.samples if s["gpu_watts"] is not None})
        cpu_modeled = sum(1 for s in self.samples if s["cpu_source"] == "modeled_from_util")

        gpu_measured = any(s["gpu_watts"] is not None for s in self.samples)
        gpu_water_fraction = None
        gpu_water_attribution_method = "unknown"
        if gpu_measured and (total_cpu_wh + total_gpu_wh) > 0:
            gpu_water_fraction = round(total_gpu_wh / (total_cpu_wh + total_gpu_wh), 4)
            gpu_water_attribution_method = "energy_proportional"

        from audit_pack import methodology_hash
        from measurement_grade import compute_measurement_grade, grade_caveat
        from schema_contract import make_caveat

        resolved_workload_type = infer_workload_type(
            training_steps=self.training_steps,
            token_count=self.token_count,
            request_count=self.request_count,
            image_count=self.image_count,
            explicit=self.workload_type,
        )

        meth_hash = methodology_hash()

        caveats = [
            make_caveat(
                "cpu_rapl_limitation",
                "info",
                "CPU energy measured only when Intel/AMD RAPL is accessible (most Linux bare metal). "
                "Cloud VMs typically block RAPL; on those hosts CPU watts are modeled from utilization.",
            ),
            make_caveat(
                "gpu_nvml_limitation",
                "info",
                "GPU energy measured only when NVIDIA NVML / nvidia-smi is available.",
            ),
            make_caveat(
                "water_annual_average",
                "info",
                "Water values combine direct cooling (WUE, vendor-published) and indirect generation water "
                "(NREL Macknick et al. 2012 + USGS 2020). Both are annual averages. "
                "Watermark reports modeled consumption-equivalent volumes unless operator withdrawal is declared.",
            ),
            make_caveat(
                "watershed_stress_weighting",
                "info",
                "Watershed-Weighted Liters (WWL) apply WRI Aqueduct baseline stress as a modest multiplier "
                f"({WATER_STRESS_WEIGHTING_METHOD}); basin-level data may not match facility location.",
            ),
            make_caveat(
                "pue_constant",
                "info",
                "PUE is treated as constant; real PUE varies with weather, load, and time of day.",
            ),
            make_caveat(
                "memory_not_separate",
                "info",
                "Memory (DRAM) energy is NOT separately accounted for; it is captured only insofar as RAPL "
                "package counters include the integrated memory controller.",
            ),
        ]
        accounting_method = impacts["water"]["water_accounting_method"]
        if accounting_method == WATER_ACCOUNTING_UNKNOWN:
            caveats.append(make_caveat(
                "water_accounting_unknown",
                "warning",
                "water_accounting_method is unknown — modeled totals may overstate or understate "
                "facility impact vs operator withdrawal or consumption disclosures.",
            ))
        if accounting_method == WATER_ACCOUNTING_WITHDRAWAL:
            caveats.append(make_caveat(
                "water_withdrawal_not_consumption",
                "warning",
                "Operator disclosure uses withdrawal accounting — withdrawal ≠ consumption; "
                "actual consumed water may be lower (especially with evaporative cooling).",
            ))
        if self.water_source == "operator":
            caveats.append(make_caveat(
                "operator_disclosure_self_reported",
                "warning",
                "Direct WUE from operator self-reported disclosure (disclosure_verified=false unless "
                "independently verified). Not a flow meter reading.",
            ))
        if self.cooling_type == "unknown":
            caveats.append(make_caveat(
                "cooling_type_unknown",
                "warning",
                "Cooling type unknown — direct WUE uses regional default without evaporative/air/liquid "
                "adjustment. Pass --cooling-system (evaporative, air, liquid, immersion) when known.",
            ))
        if self.water_profile.get("water_stress_season") == "annual":
            base_stress = float(REGION_PROFILES.get(self.region, {}).get("water_stress_score", 0.0))
            if base_stress > 0.6:
                caveats.append(make_caveat(
                    "water_stress_annual_high_basin",
                    "warning",
                    "Annual average stress may understate peak-season impact in this drought-prone basin "
                    f"(baseline score {base_stress:.2f}). Use --water-stress-season summer for peak analysis.",
                ))
            else:
                caveats.append(make_caveat(
                    "water_stress_season_annual",
                    "info",
                    "Basin stress uses annual Aqueduct averages; drought-prone regions vary significantly by season.",
                ))
        elif self.water_profile.get("water_stress_season") in ("summer", "fall"):
            caveats.append(make_caveat(
                "water_stress_seasonal",
                "info",
                f"Basin stress adjusted for {self.water_profile['water_stress_season']} "
                f"(as of {self.water_profile.get('water_stress_as_of', 'run start')}).",
            ))
        if embodied["source"] == "modeled":
            caveats.append(make_caveat(
                "embodied_amortized",
                "info",
                f"Embodied carbon/water amortized over {embodied['useful_life_hours']} h useful life "
                f"for SKU {embodied['sku']} ({embodied['label']}); source: {embodied['citation']}.",
            ))
        elif self.hardware_sku_requested:
            caveats.append(make_caveat(
                "embodied_no_profile",
                "warning",
                f"Embodied impact not computed: unknown or unsupported --hardware-sku "
                f"'{self.hardware_sku_requested}' (tagged no_profile).",
            ))
        else:
            caveats.append(make_caveat(
                "embodied_not_computed",
                "info",
                "Embodied (manufacturing) carbon/water not computed — pass --hardware-sku or a "
                "recognizable --hardware label (e.g. 'H100 SXM').",
            ))
        if self.profile.get("carbon_source") == CARBON_SOURCE_EM:
            ts = self.profile.get("timestamp") or "run start"
            zone = self.profile.get("em_zone") or self.region
            caveats.append(make_caveat(
                "grid_carbon_em_realtime",
                "info",
                f"Grid carbon intensity from ElectricityMaps at {ts} (zone/data-center: {zone}), "
                "tagged em_realtime.",
            ))
        else:
            caveats.append(make_caveat(
                "grid_carbon_static_avg",
                "info",
                "Grid carbon intensity is a regional annual average (static_avg). "
                "For real-time attribution use --grid-source electricitymaps.",
            ))
            caveats.append(make_caveat(
                "grid_intensity_annual_average",
                "warning",
                "Grid carbon/water intensities are annual regional averages; short workloads may not align "
                "with marginal dispatch at the time of execution.",
            ))
        if cpu_modeled > 0:
            caveats.append(make_caveat(
                "cpu_modeled_from_util",
                "warning",
                f"CPU modeled with assumed TDP {self.cpu_tdp_fallback} W "
                f"({cpu_modeled}/{len(self.samples)} samples); verify against instance SKU or SPECpower "
                "for publishable results.",
            ))
        if per_unit and self.normalization_source and self.normalization_source != "cli":
            caveats.append(make_caveat(
                "per_unit_from_workload_metrics",
                "info",
                f"per_unit normalized using {per_unit['unit_count']} {per_unit['unit_type']}(s) "
                f"reported by workload ({self.normalization_source}).",
            ))

        draft_summary = {
            "run_metadata": {"samples": len(self.samples)},
            "measured_sources": {
                "cpu": cpu_sources,
                "gpu": gpu_sources if gpu_sources else ["no_gpu"],
                "cpu_rapl_samples": sum(1 for s in self.samples if s["cpu_source"] == "rapl_measured"),
                "cpu_modeled_samples": cpu_modeled,
                "gpu_missing_samples": sum(1 for s in self.samples if s["gpu_watts"] is None),
            },
            "assumptions": {
                "region": self.region,
                "cooling_type": self.cooling_type,
                "pue_source": self.pue_source,
                "wue_source": self.wue_source,
            },
            "water": {"water_accounting_method": accounting_method},
        }
        measurement_grade, grade_limiting_factor = compute_measurement_grade(
            draft_summary,
            rapl_available_at_start=self.rapl_available_at_start,
        )
        grade_cv = grade_caveat(measurement_grade, grade_limiting_factor)
        if grade_cv:
            caveats.append(grade_cv)

        return {
            "schema_version": "0.3",
            "measurement_grade": measurement_grade,
            "grade_limiting_factor": grade_limiting_factor,
            "per_unit": per_unit,
            "run_metadata": {
                "started_at_utc": self.samples[0]["timestamp"],
                "ended_at_utc": self.samples[-1]["timestamp"],
                "duration_s": duration_s,
                "samples": len(self.samples),
                "host_os": platform.platform(),
                "rapl_platform": self.rapl.platform,
                "scope": self.scope,
                "workload_type": resolved_workload_type,
                "methodology_hash": meth_hash,
            },
            "measured_sources": {
                "cpu": cpu_sources,
                "gpu": gpu_sources if gpu_sources else ["no_gpu"],
                "cpu_rapl_samples": sum(1 for s in self.samples if s["cpu_source"] == "rapl_measured"),
                "cpu_modeled_samples": cpu_modeled,
                "gpu_missing_samples": sum(1 for s in self.samples if s["gpu_watts"] is None),
            },
            "energy": {
                "it_cpu_wh": round(total_cpu_wh, 4),
                "it_cpu_source": cpu_sources,
                "it_gpu_wh": round(total_gpu_wh, 4),
                "it_gpu_source": gpu_sources if gpu_sources else ["no_gpu"],
                "it_total_kwh": round(it_kwh, 6),
                "it_total_source": "sum_it_components",
                "facility_total_kwh": round(facility_kwh, 6),
                "facility_source": impacts["energy"]["facility_source"],
            },
            "carbon": {
                "co2e_kg": round(impacts["carbon"]["co2e_kg"], 6),
                "source": impacts["carbon"]["source"],
                "energy_basis_kwh": round(impacts["carbon"]["energy_basis_kwh"], 6),
            },
            "water": {
                "direct_cooling_l": round(impacts["water"]["direct_cooling_l"], 4),
                "indirect_generation_l": round(impacts["water"]["indirect_generation_l"], 4),
                "total_l": round(impacts["water"]["total_l"], 4),
                "direct_source": impacts["water"]["direct_source"],
                "indirect_source": impacts["water"]["indirect_source"],
                "stress_score": impacts["water"]["stress_score"],
                "stress_level": impacts["water"]["stress_level"],
                "stress_basin": impacts["water"]["stress_basin"],
                "stress_source": impacts["water"]["stress_source"],
                "stress_weighted_total_l": round(impacts["water"]["stress_weighted_total_l"], 4),
                "wwl_l": round(impacts["water"]["wwl_l"], 6),
                "wwl_ml": round(impacts["water"]["wwl_ml"], 4),
                "wwl_per_unit_ml": impacts["water"].get("wwl_per_unit_ml"),
                "water_accounting_method": impacts["water"]["water_accounting_method"],
                "water_source": impacts["water"]["water_source"],
                "weighting_methodology": impacts["water"]["weighting_methodology"],
                "gpu_water_fraction": gpu_water_fraction,
                "gpu_water_attribution_method": gpu_water_attribution_method,
            },
            "embodied": {
                "co2e_kg": embodied["co2e_kg"],
                "water_l": embodied["water_l"],
                "embodied_water_ml": embodied.get("embodied_water_ml"),
                "sku": embodied["sku"],
                "useful_life_hours": embodied["useful_life_hours"],
                "source": embodied["source"],
                "citation": embodied["citation"],
                "label": embodied["label"],
            },
            "lifecycle": {
                "carbon": {
                    "operational_kg": round(impacts["carbon"]["co2e_kg"], 6),
                    "embodied_kg": embodied["co2e_kg"],
                    "total_kg": round(impacts["carbon"]["co2e_kg"] + embodied["co2e_kg"], 6),
                },
                "water": {
                    "operational_l": round(impacts["water"]["total_l"], 4),
                    "embodied_l": embodied["water_l"],
                    "total_l": round(impacts["water"]["total_l"] + embodied["water_l"], 4),
                },
            },
            "assumptions": {
                "region": self.region,
                "region_label": self.profile["label"],
                "grid_source": self.grid_source,
                "grid_profile_source": self.profile.get("source", CARBON_SOURCE_STATIC),
                "carbon_intensity_source": self.profile.get("carbon_source", CARBON_SOURCE_STATIC),
                "carbon_intensity_timestamp": self.profile.get("timestamp"),
                "em_zone": self.profile.get("em_zone"),
                "pue": self.pue,
                "pue_source": self.pue_source,
                "wue_direct_l_per_kwh": self.wue_direct,
                "wue_source": self.wue_source,
                "grid_co2_kg_per_kwh": self.profile["co2_kg_per_kwh"],
                "grid_water_l_per_kwh": self.water_profile["water_l_per_kwh"],
                "water_source": self.water_source,
                "water_accounting_method": impacts["water"]["water_accounting_method"],
                "water_stress_season": self.water_profile.get("water_stress_season"),
                "water_stress_as_of": self.water_profile.get("water_stress_as_of"),
                "cooling_type": self.cooling_type,
                "operator_disclosure_source": self.water_profile.get("operator_disclosure_source"),
                "disclosure_verified": self.water_profile.get("disclosure_verified"),
                "disclosure_type": self.water_profile.get("disclosure_type"),
                "disclosure_year": self.water_profile.get("disclosure_year"),
                "carbon_energy_basis": "facility_kwh",
                "carbon_rationale": "Scope 2: grid intensity applied to total facility electricity (IT × PUE).",
                "cpu_tdp_fallback_w": self.cpu_tdp_fallback,
                "hardware_sku": self.hardware_sku,
                "hardware_sku_requested": self.hardware_sku_requested,
            },
            "caveats": caveats,
        }

    def write_outputs(self, summary, workload=None, write_dashboard=False,
                      compare_summary=None, compare_samples=None, compare_workload=None):
        csv_path = self.output_dir / "measurements.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(self.samples[0].keys()))
            writer.writeheader()
            for s in self.samples:
                writer.writerow(s)

        json_path = self.output_dir / "summary.json"
        json_path.write_text(json.dumps(summary, indent=2))

        md_path = self.output_dir / "report.md"
        md_path.write_text(self._render_markdown(summary))

        from dashboard import generate_dashboard, save_workload_metadata
        workload_path = save_workload_metadata(self.output_dir, workload)

        dashboard_path = None
        if write_dashboard:
            dashboard_path = generate_dashboard(
                summary, self.samples, self.output_dir, workload=workload,
                compare_summary=compare_summary,
                compare_samples=compare_samples,
                compare_workload=compare_workload,
            )

        return csv_path, json_path, md_path, workload_path, dashboard_path

    @staticmethod
    def _format_source_list(sources) -> str:
        if isinstance(sources, str):
            return sources
        return ", ".join(sources)

    @staticmethod
    def _format_cpu_sources(meas: dict) -> str:
        parts = []
        if meas.get("cpu_rapl_samples"):
            parts.append(f"rapl_measured: {meas['cpu_rapl_samples']} samples")
        if meas.get("cpu_modeled_samples"):
            parts.append(f"modeled_from_util: {meas['cpu_modeled_samples']} samples")
        return ", ".join(parts) if parts else Meter._format_source_list(meas.get("cpu", ["unknown"]))

    @staticmethod
    def _render_markdown(s):
        e, c, w, a = s["energy"], s["carbon"], s["water"], s["assumptions"]
        emb = s.get("embodied", {})
        lc = s.get("lifecycle", {})
        meas = s["measured_sources"]
        rm = s["run_metadata"]
        cpu_tag = Meter._format_cpu_sources(meas)
        gpu_tag = Meter._format_source_list(e.get("it_gpu_source", meas.get("gpu", ["no_gpu"])))
        carbon_tag = c.get("source", a.get("carbon_intensity_source", CARBON_SOURCE_STATIC))
        water_grid_tag = w.get("indirect_source", CARBON_SOURCE_STATIC)

        lines = [
            "# Workload Footprint Report",
            "",
            f"**Duration:** {rm['duration_s']} s ({rm['samples']} samples)  ",
            f"**Region:** `{a['region']}` — {a['region_label']}  ",
            f"**Host:** {rm['host_os']}",
            "",
            "## Energy",
            "",
            f"- CPU: **{e['it_cpu_wh']} Wh**  _({cpu_tag})_",
            f"- GPU: **{e['it_gpu_wh']} Wh**  _({gpu_tag})_",
            f"- IT total: **{e['it_total_kwh']} kWh**  _({e.get('it_total_source', 'sum_it_components')})_",
            f"- Facility total (× PUE {a['pue']}): **{e['facility_total_kwh']} kWh**  "
            f"_({e.get('facility_source', 'pue_multiplier')}, {a.get('pue_source', 'default_iea_2024')})_",
            "",
            "## Carbon",
            "",
            f"- Operational CO₂e: **{c['co2e_kg']} kg**  _({carbon_tag}, basis: {a.get('carbon_energy_basis', 'facility_kwh')})_",
            f"- Grid intensity used: {a['grid_co2_kg_per_kwh']} kgCO₂e/kWh  _({carbon_tag})_",
        ]
        if emb.get("source") == "modeled":
            lines.extend([
                f"- Embodied CO₂e (amortized): **{emb['co2e_kg']} kg**  _(modeled, amortized; SKU {emb['sku']})_",
                f"- Lifecycle CO₂e (operational + embodied): **{lc['carbon']['total_kg']} kg**",
            ])
        elif emb.get("source") == "no_profile":
            lines.append(
                f"- Embodied CO₂e: **not computed**  _(no_profile; requested SKU: {emb.get('sku') or 'none'})_"
            )
        lines.extend([
            "",
            "## Water",
            "",
            f"- Direct cooling (WUE {a['wue_direct_l_per_kwh']} L/kWh IT): **{w['direct_cooling_l']} L**  "
            f"_({w.get('direct_source', 'modeled_wue')}, {a.get('wue_source', 'default_vendor_avg')})_",
            f"- Indirect generation ({a['grid_water_l_per_kwh']} L/kWh facility): **{w['indirect_generation_l']} L**  "
            f"_({water_grid_tag})_",
            f"- Operational total: **{w['total_l']} L**  _(sum_water_components)_",
        ])
        if w.get("stress_weighted_total_l") is not None:
            lines.append(
                f"- Stress-weighted total: **{w['stress_weighted_total_l']} L**  "
                f"_(WRI Aqueduct; basin {w.get('stress_basin', 'n/a')}; "
                f"score {w.get('stress_score', 0)}; {w.get('weighting_methodology', '')})_"
            )
        if emb.get("source") == "modeled":
            lines.extend([
                f"- Embodied freshwater (amortized): **{emb['water_l']} L**  _(modeled, amortized; SKU {emb['sku']})_",
                f"- Lifecycle water (operational + embodied): **{lc['water']['total_l']} L**",
            ])
        elif emb.get("source") == "no_profile":
            lines.append(
                f"- Embodied water: **not computed**  _(no_profile)_"
            )
        lines.extend([
            "",
            "## Caveats",
            "",
        ])
        from schema_contract import caveat_message

        for cv in s["caveats"]:
            lines.append(f"- {caveat_message(cv)}")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "Measure compute's water cost — direct, indirect, and watershed-weighted — "
            "plus energy and carbon for AI workloads."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Hero comparison — structured regional tradeoff:\n"
            "  watermark compare-regions --workload embeddings "
            "--regions us-east-1 eu-north-1 us-west-2 --output comparison.json\n"
            "  watermark --region us-east-1 --output ./run_va --duration 60\n"
            "  watermark portfolio ./experiments --output portfolio.html\n"
            "You optimized for carbon. Did you check water?"
        ),
    )
    p.add_argument("--region", default="global-avg", choices=sorted(REGION_PROFILES.keys()),
                   help="Grid/cloud region for carbon and water intensity.")
    p.add_argument("--pue", type=float, default=DEFAULT_PUE,
                   help=f"Power Usage Effectiveness (default: {DEFAULT_PUE}).")
    p.add_argument("--wue", type=float, default=None,
                   help="Override direct WUE in L/kWh IT (default: region profile from vendor report).")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Sampling interval in seconds (default: 1.0).")
    p.add_argument("--cpu-tdp", type=float, default=65.0,
                   help="CPU TDP in watts to use when RAPL is unavailable (default: 65).")
    p.add_argument("--duration", type=float, default=None,
                   help="Standalone measurement duration in seconds. Ignored if a command is given.")
    p.add_argument("--output", default="./watermark_run",
                   help="Output directory (default: ./watermark_run).")
    p.add_argument("--scope", default="host", choices=["host"],
                   help="Measurement scope (host only in v0.1; per-process is v0.3).")
    p.add_argument("--grid-source", default="static", choices=["static", "electricitymaps"],
                   dest="grid_source",
                   help="Grid carbon intensity source: static regional table (default) or ElectricityMaps API.")
    p.add_argument("--water-source", default="static", choices=["static", "operator"],
                   dest="water_source",
                   help="Water profile source: static regional tables (default) or operator disclosures.")
    p.add_argument("--water-stress-season", default=None, dest="water_stress_season",
                   choices=["annual", "winter", "spring", "summer", "fall"],
                   help="Season for basin stress multiplier (default: annual).")
    p.add_argument("--workload-type", default=None, dest="workload_type",
                   choices=["training", "inference", "benchmark", "unknown"],
                   help="Declare workload type for portfolio normalization guards.")
    p.add_argument("--dashboard", action="store_true",
                   help="Write dashboard.html (also auto-enabled with --run-id or --image-count).")
    p.add_argument("--no-dashboard", action="store_true",
                   help="Skip dashboard.html even when experiment metadata is set.")
    p.add_argument("--run-id", default=None,
                   help="Run identifier shown on the dashboard (default: derived from region + samples).")
    p.add_argument("--workload-name", default=None,
                   help="Workload label for the dashboard, e.g. 'SDXL · 100 imgs'.")
    p.add_argument("--image-count", type=int, default=None,
                   help="Number of images/units for per-item footprint on the dashboard.")
    p.add_argument("--token-count", type=int, default=None, dest="token_count",
                   help="Token count for per-token WWL/energy/carbon normalization.")
    p.add_argument("--request-count", type=int, default=None, dest="request_count",
                   help="Request count for per-request normalization.")
    p.add_argument("--training-steps", type=int, default=None, dest="training_steps",
                   help="Training steps for per-step normalization (training workloads).")
    p.add_argument("--hardware", default=None,
                   help="Hardware label for the dashboard sidebar, e.g. 'H100 SXM 80GB'.")
    p.add_argument("--hardware-sku", default=None, dest="hardware_sku",
                   help="Embodied-impact hardware profile: h100-sxm, a100, generic, default-gpu.")
    p.add_argument("--steps", type=int, default=None,
                   help="Inference/training steps per unit (dashboard metadata).")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed (dashboard metadata).")
    p.add_argument("--compute-rate", type=float, default=None, dest="compute_rate",
                   help="USD per hour for compute cost KPI on the dashboard.")
    p.add_argument("--model", default=None,
                   help="Model ID or label for workload.json (e.g. meta-llama/Llama-3.2-3B-Instruct).")
    p.add_argument("--facility-location", default=None, dest="facility_location",
                   help="Physical site label for workload.json (e.g. 'Iceland (RunPod EUR-IS-3)').")
    p.add_argument("--notes", default=None,
                   help="Free-form run notes stored in workload.json and shown on the dashboard.")
    p.add_argument("--cooling-system", default=None, dest="cooling_system",
                   choices=["evaporative", "air", "dry", "liquid", "immersion"],
                   help="Facility cooling type — adjusts direct WUE (evaporative, air, liquid, immersion).")
    p.add_argument("--compare-run", default=None, dest="compare_run",
                   help="Path to another run's summary.json (or run directory) for side-by-side dashboard comparison.")
    p.add_argument("--portfolio-dir", type=Path, default=None, dest="portfolio_dir",
                   help="Portfolio workspace to refresh: alone = generate only; with a run = auto-update after measurement.")
    p.add_argument("--no-portfolio-update", action="store_true", dest="no_portfolio_update",
                   help="Skip auto portfolio refresh after a measurement run.")
    p.add_argument("--sort-by", default="timestamp",
                   choices=["timestamp", "region", "workload", "carbon", "water", "wwl", "cost", "energy"],
                   dest="sort_by",
                   help="Portfolio default sort order (with --portfolio-dir).")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="Optional command to run while measuring. Prefix with --.")
    args = p.parse_args()

    if args.interval <= 0:
        p.error("--interval must be > 0")
    if args.pue < 1.0:
        p.error("--pue must be >= 1.0 (ratio of facility to IT energy)")
    if args.wue is not None and args.wue < 0:
        p.error("--wue must be >= 0")
    if args.cpu_tdp <= 0:
        p.error("--cpu-tdp must be > 0")
    if args.duration is not None and args.duration <= 0:
        p.error("--duration must be > 0")
    if args.image_count is not None and args.image_count <= 0:
        p.error("--image-count must be > 0")
    if args.token_count is not None and args.token_count <= 0:
        p.error("--token-count must be > 0")
    if args.request_count is not None and args.request_count <= 0:
        p.error("--request-count must be > 0")
    if args.training_steps is not None and args.training_steps <= 0:
        p.error("--training-steps must be > 0")
    if args.compute_rate is not None and args.compute_rate < 0:
        p.error("--compute-rate must be >= 0")

    args.pue_source = "user_override" if "--pue" in sys.argv else "default_iea_2024"
    args.wue_source = "user_override" if "--wue" in sys.argv else None
    return args


def main():
    if len(sys.argv) > 1:
        sub = sys.argv[1]
        if sub == "portfolio":
            from dashboard.portfolio import portfolio_cli_main
            raise SystemExit(portfolio_cli_main(sys.argv[2:]))
        if sub == "audit-pack":
            from audit_pack import audit_pack_cli_main
            raise SystemExit(audit_pack_cli_main(sys.argv[2:]))
        if sub == "compare-regions":
            from compare_regions import compare_regions_cli_main
            raise SystemExit(compare_regions_cli_main(sys.argv[2:]))
        if sub == "gate":
            from gate import gate_cli_main
            raise SystemExit(gate_cli_main(sys.argv[2:]))
        if sub == "annotate":
            from annotate import annotate_cli_main
            raise SystemExit(annotate_cli_main(sys.argv[2:]))

    args = parse_args()
    _warn_em_key_if_static(args.grid_source)
    _warn_operator_disclosure_if_static(args.region, args.water_source)

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if args.portfolio_dir and not cmd and args.duration is None:
        from dashboard.portfolio import discover_run_dirs, generate_portfolio, resolve_portfolio_output
        scan_dir = Path(args.portfolio_dir).resolve()
        out = resolve_portfolio_output(args.output)
        path = generate_portfolio(scan_dir, out, sort_by=args.sort_by)
        print(f"[watermark] portfolio: found {len(discover_run_dirs(scan_dir))} run(s)")
        print(f"[watermark] wrote {path}")
        return 0

    sku_requested = _normalize_hardware_sku(args.hardware_sku) if args.hardware_sku else None
    sku_resolved = resolve_hardware_sku(
        sku_requested,
        args.hardware or args.workload_name,
    )
    meter = Meter(
        region=args.region,
        pue=args.pue,
        wue_direct=args.wue,
        interval_s=args.interval,
        cpu_tdp_fallback=args.cpu_tdp,
        output_dir=args.output,
        scope=args.scope,
        pue_source=args.pue_source,
        wue_source=args.wue_source,
        grid_source=args.grid_source,
        water_source=args.water_source,
        cooling_type=args.cooling_system,
        hardware_sku=sku_resolved,
        hardware_sku_requested=sku_requested,
        image_count=args.image_count,
        token_count=args.token_count,
        request_count=args.request_count,
        training_steps=args.training_steps,
        workload_type=args.workload_type,
        water_stress_season=args.water_stress_season,
    )

    cpu_hint = "rapl_measured" if meter.rapl.available else "modeled_from_util"
    gpu_hint = meter.nvml.mode if meter.nvml.available else "no_gpu"
    carbon_hint = meter.profile.get("carbon_source", CARBON_SOURCE_STATIC)
    print(f"[watermark] region:       {args.region} ({REGION_PROFILES[args.region]['label']})")
    print(f"[watermark] grid source:  {args.grid_source} (carbon: {carbon_hint})")
    print(f"[watermark] water source: {args.water_source}")
    print(f"[watermark] cpu source:   {cpu_hint}" +
          (f" ({meter.rapl.platform})" if meter.rapl.platform else ""))
    print(f"[watermark] gpu source:   {gpu_hint}")
    print(f"[watermark] PUE={args.pue} ({args.pue_source})  "
          f"WUE_direct={meter.wue_direct} ({meter.wue_source})  interval={args.interval}s")
    print(f"[watermark] output:       {args.output}")
    if args.portfolio_dir:
        print(f"[watermark] portfolio:    auto-update {args.portfolio_dir.resolve()}")
    elif os.environ.get("WATERMARK_PORTFOLIO_DIR"):
        print(f"[watermark] portfolio:    auto-update {os.environ['WATERMARK_PORTFOLIO_DIR']}")
    if sku_resolved:
        print(f"[watermark] hardware SKU: {sku_resolved} (embodied amortized)")
    elif sku_requested:
        print(f"[watermark] hardware SKU: {sku_requested} (unknown — embodied=no_profile)")
    elif args.hardware:
        print(f"[watermark] hardware SKU: not recognized from --hardware (embodied skipped)")
    print()

    try:
        if cmd:
            print(f"[watermark] running: {' '.join(cmd)}")
            rc = meter.run_command(cmd)
            print(f"[watermark] command exited with code {rc}")
        elif args.duration is not None:
            print(f"[watermark] measuring for {args.duration}s ...")
            meter.run_for(args.duration)
        else:
            print("error: provide either --duration N or a command after --", file=sys.stderr)
            sys.exit(2)

        from workload_metrics import load_workload_metrics, resolve_unit_normalization

        auto_metrics = load_workload_metrics(meter.output_dir)
        _ut, _uc, norm_src = resolve_unit_normalization(
            auto_metrics,
            token_count=meter.token_count,
            request_count=meter.request_count,
            training_steps=meter.training_steps,
            image_count=meter.image_count,
        )
        if norm_src:
            meter.normalization_source = norm_src
        if _ut == "training_step" and _uc and not meter.training_steps:
            meter.training_steps = _uc
        elif _ut == "token" and _uc and not meter.token_count:
            meter.token_count = _uc
        elif _ut == "request" and _uc and not meter.request_count:
            meter.request_count = _uc
        elif _ut == "image" and _uc and not meter.image_count:
            meter.image_count = _uc

        summary = meter.aggregate()
        workload = {
            "run_id": args.run_id,
            "name": args.workload_name,
            "image_count": args.image_count,
            "hardware": args.hardware,
            "steps": args.steps,
            "seed": args.seed,
            "compute_rate_usd_hr": args.compute_rate,
            "model": args.model,
            "facility_location": args.facility_location,
            "notes": args.notes,
            "cooling_system": args.cooling_system,
        }
        from dashboard import load_compare_run, should_write_dashboard
        compare_summary = compare_samples = compare_workload = None
        if args.compare_run:
            try:
                compare_summary, compare_samples, compare_workload = load_compare_run(
                    Path(args.compare_run),
                )
            except (FileNotFoundError, ValueError, json.JSONDecodeError, KeyError) as exc:
                print(f"error: --compare-run: {exc}", file=sys.stderr)
                sys.exit(2)
        write_dashboard = should_write_dashboard(
            run_id=args.run_id,
            image_count=args.image_count,
            dashboard=args.dashboard,
            no_dashboard=args.no_dashboard,
            compare_run=args.compare_run,
        )
        csv_path, json_path, md_path, workload_path, dashboard_path = meter.write_outputs(
            summary,
            workload=workload,
            write_dashboard=write_dashboard,
            compare_summary=compare_summary,
            compare_samples=compare_samples,
            compare_workload=compare_workload,
        )

        print()
        print(f"[watermark] wrote {csv_path}")
        print(f"[watermark] wrote {json_path}")
        w = summary["water"]
        wwl_ml = w.get("wwl_ml", w.get("stress_weighted_total_l", 0) * 1000)
        print(f"[watermark] WWL:          {wwl_ml:.1f} mL (watershed-weighted water)")
        print(f"[watermark] grade:        {summary.get('measurement_grade', '?')}"
              + (f" ({summary.get('grade_limiting_factor')})" if summary.get('grade_limiting_factor') else ""))
        print(f"[watermark] carbon:       {summary['carbon']['co2e_kg'] * 1000:.2f} g CO₂e")
        print(
            "[watermark] tip: compare us-east-1, eu-north-1, us-west-1 — "
            "you optimized for carbon. Did you check water?"
        )
        print(f"[watermark] wrote {md_path}")
        if workload_path:
            print(f"[watermark] wrote {workload_path}")
        if dashboard_path:
            print(f"[watermark] wrote {dashboard_path}")
        elif not write_dashboard:
            print("[watermark] dashboard skipped (use --dashboard or pass --run-id / --image-count)")

        if not args.no_portfolio_update:
            from dashboard.portfolio import discover_run_dirs, regenerate_portfolio_after_run
            portfolio_path = regenerate_portfolio_after_run(
                Path(args.output),
                portfolio_dir=args.portfolio_dir,
                sort_by=args.sort_by,
            )
            if portfolio_path is not None:
                count = len(discover_run_dirs(portfolio_path.parent))
                print(f"[watermark] portfolio: refreshed ({count} run(s) scanned)")
                print(f"[watermark] wrote {portfolio_path}")

        print()
        print(md_path.read_text())
    finally:
        meter.nvml.shutdown()


if __name__ == "__main__":
    main()
