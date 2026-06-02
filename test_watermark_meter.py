"""Tests for watermark_meter grid profiles and ElectricityMaps zone mappings."""

from __future__ import annotations

import os

import pytest

import watermark_meter as wm


class TestRegionProfiles:
    NEW_REGIONS = (
        "sa-east-1",
        "ca-central-1",
        "ap-southeast-2",
        "ap-east-1",
        "ap-northeast-2",
    )

    REQUIRED_FIELDS = (
        "label",
        "co2_kg_per_kwh",
        "water_l_per_kwh",
        "wue_direct_l_per_kwh",
        "wue_source",
        "water_stress_score",
        "water_stress_level",
        "water_stress_basin",
        "stress_source",
    )

    def test_new_regions_have_required_profile_fields(self):
        for region in self.NEW_REGIONS:
            profile = wm.REGION_PROFILES[region]
            for field in self.REQUIRED_FIELDS:
                assert field in profile, f"{region} missing {field}"

    def test_new_regions_have_em_zone_mappings(self):
        for region in self.NEW_REGIONS:
            assert region in wm.EM_ZONE_MAP, f"{region} missing from EM_ZONE_MAP"


class TestEmZoneMap:
    # requires network: EM /v3/zones endpoint (live Electricity Maps zone list)
    @pytest.mark.skipif(
        not os.environ.get("ELECTRICITYMAPS_API_KEY"),
        reason="requires network: EM /v3/zones endpoint (set ELECTRICITYMAPS_API_KEY to enable)",
    )
    def test_em_zone_map_codes_exist_in_v3_zones(self):
        valid_zones = wm.fetch_em_zone_keys()
        invalid = wm.invalid_em_zone_map_entries(valid_zones)
        assert invalid == [], f"unknown ElectricityMaps zones: {invalid}"
