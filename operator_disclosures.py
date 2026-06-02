"""Static operator water disclosure registry (see operator_disclosures.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REGISTRY_PATH = Path(__file__).with_name("operator_disclosures.json")
_REGISTRY_CACHE: dict[str, list[dict[str, Any]]] | None = None

REQUIRED_FIELDS = (
    "region",
    "operator",
    "wue_l_per_kwh",
    "accounting_method",
    "disclosure_source",
    "disclosure_type",
    "disclosure_verified",
    "disclosure_year",
)


def _load_registry() -> dict[str, list[dict[str, Any]]]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    by_region: dict[str, list[dict[str, Any]]] = {}
    for entry in raw.get("disclosures", []):
        for field in REQUIRED_FIELDS:
            if field not in entry:
                raise ValueError(f"operator disclosure missing {field!r}: {entry.get('region')}")
        if entry["accounting_method"] not in {"consumption", "withdrawal", "unknown"}:
            raise ValueError(f"invalid accounting_method in disclosure for {entry['region']}")
        by_region.setdefault(entry["region"], []).append(entry)
    _REGISTRY_CACHE = by_region
    return by_region


def regions_with_disclosures() -> set[str]:
    return set(_load_registry().keys())


def get_operator_disclosure(region: str, operator: str | None = None) -> dict[str, Any] | None:
    """Return disclosure for region; prefer AWS when multiple operators exist."""
    entries = _load_registry().get(region, [])
    if not entries:
        return None
    if operator:
        for entry in entries:
            if entry["operator"] == operator:
                return entry.copy()
    for preferred in ("aws", "azure", "google"):
        for entry in entries:
            if entry["operator"] == preferred:
                return entry.copy()
    return entries[0].copy()


def list_disclosures_for_region(region: str) -> list[dict[str, Any]]:
    return [e.copy() for e in _load_registry().get(region, [])]
