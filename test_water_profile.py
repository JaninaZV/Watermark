"""Tests for fetch_water_profile, WWL, and cooling-system WUE adjustments."""

from __future__ import annotations

import datetime as dt

import pytest

import watermark_meter as wm


class TestFetchWaterProfile:
    def test_static_profile_has_accounting_method(self):
        at = dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc)
        profile = wm.fetch_water_profile("us-east-1", at=at)
        assert profile["accounting_method"] == wm.WATER_ACCOUNTING_CONSUMPTION
        assert profile["water_source"] == wm.WATER_SOURCE_STATIC
        assert profile["water_stress_season"] == "annual"

    def test_operator_profile_withdrawal(self):
        profile = wm.fetch_water_profile("us-east-1", water_source="operator")
        assert profile["water_source"] == wm.WATER_SOURCE_OPERATOR
        assert profile["accounting_method"] == wm.WATER_ACCOUNTING_WITHDRAWAL
        assert profile["operator_disclosure_source"]

    def test_liquid_cooling_reduces_wue(self):
        at = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
        base = wm.fetch_water_profile("us-east-1", at=at, cooling_type="unknown")
        liquid = wm.fetch_water_profile("us-east-1", at=at, cooling_type="liquid")
        assert liquid["wue_direct_l_per_kwh"] < base["wue_direct_l_per_kwh"]
        assert liquid["cooling_type"] == "liquid"

    def test_compute_impacts_emits_wwl(self):
        grid = wm.static_grid_profile("us-east-1")
        water = wm.fetch_water_profile("us-east-1", cooling_type="evaporative")
        impacts = wm.compute_impacts(0.01, 0.0158, grid, water, wue_direct=water["wue_direct_l_per_kwh"])
        assert impacts["water"]["wwl_ml"] > impacts["water"]["total_l"] * 1000
        assert impacts["water"]["water_accounting_method"] == wm.WATER_ACCOUNTING_CONSUMPTION


class TestCoolingMultipliers:
    def test_air_is_sixty_percent_of_evaporative(self):
        at = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
        evap = wm.fetch_water_profile("us-east-1", at=at, cooling_type="evaporative")
        air = wm.fetch_water_profile("us-east-1", at=at, cooling_type="air")
        assert air["wue_direct_l_per_kwh"] == pytest.approx(evap["wue_direct_l_per_kwh"] * 0.6)

    def test_liquid_is_twenty_percent_of_evaporative(self):
        at = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
        evap = wm.fetch_water_profile("us-east-1", at=at, cooling_type="evaporative")
        liquid = wm.fetch_water_profile("us-east-1", at=at, cooling_type="liquid")
        assert liquid["wue_direct_l_per_kwh"] == pytest.approx(evap["wue_direct_l_per_kwh"] * 0.2)


class TestNormalizeCoolingType:
    def test_chilled_water_maps_to_liquid(self):
        assert wm.normalize_cooling_type("chilled-water") == "liquid"
