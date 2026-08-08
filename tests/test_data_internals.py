import json
from pathlib import Path

import pytest
import requests

from dane_meteo_stacje.data import (
    NoaaAuthError,
    NoaaClient,
    NoaaNetworkError,
    NoaaPayloadError,
    NoaaRateLimitError,
    _extract_city_from_noaa_item,
    _infer_country_from_noaa_item,
    _normalize_noaa_payload,
    _normalize_station,
    fetch_stations_with_cache_details,
    load_stations,
)


class _DummyResponse:
    def __init__(self, status_code: int, payload=None, headers=None, json_error: Exception | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _DummySession:
    def __init__(self, events):
        self._events = list(events)

    def get(self, url, timeout, headers):
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


def test_noaa_client_retries_429_then_succeeds():
    client = NoaaClient(max_retries_rate_limit=1, max_retries_server_error=0, backoff_seconds=0, jitter_seconds=0)
    client._session = _DummySession(
        [
            _DummyResponse(429),
            _DummyResponse(200, payload={"results": []}, headers={"ETag": "e1", "Last-Modified": "lm1"}),
        ]
    )

    payload, status, headers = client.fetch_json("https://example.invalid", token="t1")

    assert status == 200
    assert payload == {"results": []}
    assert headers["etag"] == "e1"
    assert headers["last_modified"] == "lm1"


def test_noaa_client_raises_auth_error():
    client = NoaaClient(max_retries_rate_limit=0, max_retries_server_error=0, backoff_seconds=0, jitter_seconds=0)
    client._session = _DummySession([_DummyResponse(401)])

    with pytest.raises(NoaaAuthError):
        client.fetch_json("https://example.invalid", token="t1")


def test_noaa_client_raises_rate_limit_when_retry_exhausted():
    client = NoaaClient(max_retries_rate_limit=0, max_retries_server_error=0, backoff_seconds=0, jitter_seconds=0)
    client._session = _DummySession([_DummyResponse(429)])

    with pytest.raises(NoaaRateLimitError):
        client.fetch_json("https://example.invalid", token="t1")


def test_noaa_client_raises_payload_error_for_invalid_json():
    client = NoaaClient(max_retries_rate_limit=0, max_retries_server_error=0, backoff_seconds=0, jitter_seconds=0)
    client._session = _DummySession([_DummyResponse(200, json_error=ValueError("bad json"))])

    with pytest.raises(NoaaPayloadError):
        client.fetch_json("https://example.invalid", token="t1")


def test_noaa_client_raises_network_error_after_request_exception():
    client = NoaaClient(max_retries_rate_limit=0, max_retries_server_error=0, backoff_seconds=0, jitter_seconds=0)
    client._session = _DummySession([requests.RequestException("boom")])

    with pytest.raises(NoaaNetworkError):
        client.fetch_json("https://example.invalid", token="t1")


def test_extract_city_uses_location_and_coordinates_fallbacks():
    city = _extract_city_from_noaa_item({"location": {"state": "Mazowieckie"}}, "WARSAW STATION")
    assert city == "Mazowieckie"

    city_coords = _extract_city_from_noaa_item(
        {"coordinates": {"latitude": 52.2, "longitude": 21.0}},
        "",
    )
    assert city_coords == "Lat 52.2, Lon 21.0"


def test_extract_city_heuristics_for_special_names():
    assert _extract_city_from_noaa_item({}, "BALTIMORE WASHINGTON INTL AP") == "Baltimore"
    assert _extract_city_from_noaa_item({}, "NEWARK INTERNATIONAL AIRPORT") == "Newark"


def test_infer_country_fallbacks():
    assert _infer_country_from_noaa_item({"country": "Poland"}, "PLM00012295") == "Poland"
    assert _infer_country_from_noaa_item({"location": {"country": "Germany"}}, "DEM00000001") == "Germany"
    assert _infer_country_from_noaa_item({}, "USW00014898") == "USA"
    assert _infer_country_from_noaa_item({}, "PLM00012295") == "Poland"


def test_infer_country_maps_two_letter_country_code_to_name():
    assert _infer_country_from_noaa_item({"country": "PL"}, "PLM00012295") == "Poland"


def test_infer_country_uses_station_prefix_mapping_for_pl():
    assert _infer_country_from_noaa_item({}, "PL000012120") == "Poland"


def test_normalize_station_rejects_invalid_objects():
    assert _normalize_station("x") is None
    assert _normalize_station({"station_id": "A"}) is None


def test_load_stations_accepts_dict_with_stations_and_invalid_json(tmp_path):
    stations_file = tmp_path / "stations.json"
    stations_file.write_text(
        json.dumps({"stations": [{"station_id": "A1", "city": "X", "name": "N", "country": "PL"}]}),
        encoding="utf-8",
    )
    loaded = load_stations(stations_file)
    assert loaded[0]["station_id"] == "A1"

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not-json", encoding="utf-8")
    assert load_stations(bad_file) == []


def test_normalize_noaa_payload_returns_empty_for_unsupported_shape():
    assert _normalize_noaa_payload("bad-shape") == []


def test_normalize_noaa_payload_collects_geo_stats():
    stats: dict[str, int] = {}
    stations = _normalize_noaa_payload(
        {
            "results": [
                {"id": "PL0001", "name": "Leba", "country": "PL", "latitude": 54.75, "longitude": 17.55},
                {"id": "PL0002", "name": "No Geo", "country": "PL"},
                {"id": "PL0003", "name": "Bad Geo", "country": "PL", "latitude": 120.0, "longitude": 10.0},
            ]
        },
        stats=stats,
    )

    assert len(stations) == 3
    assert stations[0]["country"] == "Poland"
    assert stations[0]["latitude"] == 54.75
    assert stats["geo_valid"] == 1
    assert stats["geo_missing"] == 1
    assert stats["geo_out_of_range"] == 1


def test_fetch_stations_with_cache_returns_sample_default_without_remote(tmp_path):
    result = fetch_stations_with_cache_details(cache_path=Path(tmp_path / "cache.json"), remote_url=None)
    assert result.source == "sample-default"
    assert result.metadata["warning"] == "No remote source configured"
