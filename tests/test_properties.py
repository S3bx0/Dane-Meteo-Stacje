import csv
import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dane_meteo_stacje.cli import search_stations
from dane_meteo_stacje.data import (
    NoaaAuthError,
    NoaaClientError,
    NoaaNetworkError,
    NoaaPayloadError,
    NoaaRateLimitError,
    NoaaTimeoutError,
    StationRecord,
    _normalize_noaa_payload,
    export_stations,
)
from dane_meteo_stacje.diagnostics import fetch_error_code, render_fetch_error

safe_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1).filter(str.strip)
coordinates = st.one_of(
    st.none(),
    st.tuples(
        st.floats(min_value=-200, max_value=200, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-300, max_value=300, allow_nan=False, allow_infinity=False),
    ),
)


@given(st.lists(st.tuples(safe_text, safe_text, coordinates), max_size=30))
def test_noaa_normalization_preserves_valid_rows_and_geo_invariants(rows):
    payload_rows = []
    for station_id, name, geo in rows:
        item = {"id": station_id, "name": name, "country": "PL"}
        if geo is not None:
            item["latitude"], item["longitude"] = geo
        payload_rows.append(item)

    stats: dict[str, int] = {}
    normalized = _normalize_noaa_payload({"results": payload_rows}, stats=stats)

    assert len(normalized) == len(rows)
    assert stats["items_total"] == len(rows)
    assert stats["items_valid"] + stats["items_invalid"] == stats["items_total"]
    assert stats["geo_valid"] + stats["geo_missing"] + stats["geo_out_of_range"] == stats["items_valid"]
    for station in normalized:
        assert station["station_id"].strip()
        assert station["name"].strip()
        assert station["country"] == "Poland"
        if "latitude" in station:
            assert -90 <= station["latitude"] <= 90
            assert -180 <= station["longitude"] <= 180


station_records = st.builds(
    StationRecord,
    station_id=safe_text,
    city=safe_text,
    name=safe_text,
    country=safe_text,
)


@given(st.lists(station_records, max_size=30), safe_text, st.integers(min_value=0, max_value=30))
def test_search_results_match_query_sort_and_limit(stations, query, limit):
    results = search_stations(query, stations=stations, limit=limit)
    normalized_query = query.strip().lower()

    assert len(results) <= limit
    assert all(
        normalized_query in str(station["name"]).lower()
        or normalized_query in str(station["city"]).lower()
        for station in results
    )
    assert results == sorted(results, key=lambda station: str(station.get("city", "")).lower())


@settings(deadline=None)
@given(stations=st.lists(station_records, max_size=20))
def test_json_and_csv_exports_round_trip(stations):
    with tempfile.TemporaryDirectory() as directory:
        json_path = Path(directory) / "stations.json"
        csv_path = Path(directory) / "stations.csv"

        export_stations(stations, output_json=json_path, output_csv=csv_path)

        assert json.loads(json_path.read_text(encoding="utf-8")) == stations
        if stations:
            with csv_path.open(encoding="utf-8", newline="") as handle:
                assert list(csv.DictReader(handle)) == stations
        else:
            assert csv_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("error", "code", "message_fragment"),
    [
        (NoaaAuthError("auth"), "NOAA_AUTH", "auth error"),
        (NoaaRateLimitError("rate"), "NOAA_RATE_LIMIT", "rate limit"),
        (NoaaPayloadError("payload"), "NOAA_PAYLOAD", "payload error"),
        (NoaaTimeoutError("timeout"), "NOAA_TIMEOUT", "timeout"),
        (NoaaNetworkError("network"), "NOAA_NETWORK", "network error"),
        (NoaaClientError("unknown"), "NOAA_UNKNOWN", "NOAA error"),
    ],
)
def test_diagnostics_contract(error, code, message_fragment):
    assert fetch_error_code(error) == code
    assert message_fragment in render_fetch_error(error)
