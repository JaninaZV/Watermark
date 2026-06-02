# Pre-existing test failures (documented)

## `test_watermark_meter.py::TestEmZoneMap::test_em_zone_map_codes_exist_in_v3_zones`

**Symptom (before fix):** `urllib.error.URLError: Tunnel connection failed: 403 Forbidden` when calling `fetch_em_zone_keys()` against `https://api.electricitymap.org/v3/zones`.

**Cause:** Test requires live network access to Electricity Maps. Fails in sandboxed or proxied environments (HTTP CONNECT 403).

**Resolution:** Test is skipped unless `ELECTRICITYMAPS_API_KEY` is set (proxy for “EM integration enabled”). CI passes the repo secret when configured; local contributors see an explicit skip reason instead of a phantom failure.

**Optional follow-up:** Mock `/v3/zones` response in unit tests; cache zone list as a checked-in fixture refreshed periodically.
