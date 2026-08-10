import json
import time
from pathlib import Path

import pytest

from dane_meteo_stacje import data
from dane_meteo_stacje.data import (
    FetchResult,
    NoaaNetworkError,
    NoaaPayloadError,
    fetch_stations_with_cache,
    fetch_stations_with_cache_details,
    read_cache_metadata,
)


def _write_cache(cache_file: Path, stations: list[dict], *, fetched_at: int, source_url: str):
    cache_file.write_text(json.dumps(stations, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_path = cache_file.with_suffix(cache_file.suffix + ".meta.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timestamp": fetched_at,
                "fetched_at": fetched_at,
                "count": len(stations),
                "source_url": source_url,
                "http_status": 200,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_cache_metadata_contains_remote_http_fields(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"

    def fake_fetch_remote_stations(remote, timeout=10, token=None, token_provider=None):
        return [
            {
                "station_id": "R1",
                "city": "Remote City",
                "name": "Remote Station",
                "country": "Poland",
            }
        ], {
            "http_status": 200,
            "payload_shape": "stations-list",
            "token_fingerprint": "abc123",
            "etag": "etag-x",
            "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        }

    monkeypatch.setattr(data, "fetch_remote_stations", fake_fetch_remote_stations)

    result = fetch_stations_with_cache_details(cache_path=cache_file, remote_url="https://example.com/stations")

    assert result.source == "remote"
    metadata = read_cache_metadata(cache_file)
    assert metadata["source_url"] == "https://example.com/stations"
    assert metadata["http_status"] == 200
    assert metadata["payload_shape"] == "stations-list"
    assert metadata["token_fingerprint"] == "abc123"
    assert metadata["etag"] == "etag-x"
    assert metadata["last_modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert isinstance(metadata["fetched_at"], int)


def test_fresh_cache_is_not_used_when_source_url_differs(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    _write_cache(
        cache_file,
        [{"station_id": "C1", "city": "Cached", "name": "Cached", "country": "Poland"}],
        fetched_at=int(time.time()),
        source_url="https://old.example.com/stations",
    )

    def fake_fetch_remote_stations(remote, timeout=10, token=None, token_provider=None):
        return [
            {
                "station_id": "R2",
                "city": "Remote New",
                "name": "Remote New",
                "country": "Poland",
            }
        ], {
            "http_status": 200,
            "payload_shape": "stations-list",
            "token_fingerprint": None,
            "etag": None,
            "last_modified": None,
        }

    monkeypatch.setattr(data, "fetch_remote_stations", fake_fetch_remote_stations)

    result = fetch_stations_with_cache_details(
        cache_path=cache_file,
        remote_url="https://new.example.com/stations",
        cache_ttl=3600,
    )

    assert result.source == "remote"
    assert result.stations[0]["station_id"] == "R2"


def test_stale_if_error_returns_cache_with_age_and_warning(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    old_ts = int(time.time()) - 7200
    _write_cache(
        cache_file,
        [{"station_id": "C2", "city": "Cached", "name": "Cached", "country": "Poland"}],
        fetched_at=old_ts,
        source_url="https://example.com/stations",
    )

    def fake_fetch_remote_stations(remote, timeout=10, token=None, token_provider=None):
        raise NoaaNetworkError("HTTP 503")

    monkeypatch.setattr(data, "fetch_remote_stations", fake_fetch_remote_stations)

    result = fetch_stations_with_cache_details(
        cache_path=cache_file,
        remote_url="https://example.com/stations",
        cache_ttl=0,
        stale_if_error=True,
    )

    assert result.source == "cache-stale"
    assert result.stations[0]["station_id"] == "C2"
    assert result.metadata["cache_age_seconds"] >= 7000
    assert "warning" in result.metadata


def test_fetch_result_contains_cache_metadata_when_used(tmp_path):
    cache_file = tmp_path / "cache.json"
    now_ts = int(time.time())
    _write_cache(
        cache_file,
        [{"station_id": "C3", "city": "Cached", "name": "Cached", "country": "Poland"}],
        fetched_at=now_ts,
        source_url="https://example.com/stations",
    )

    result: FetchResult = fetch_stations_with_cache_details(
        cache_path=cache_file,
        remote_url="https://example.com/stations",
        cache_ttl=3600,
    )

    assert result.source == "cache-fresh"
    assert result.metadata["cache_metadata"]["source_url"] == "https://example.com/stations"


def test_stale_cache_raises_when_older_than_max_stale(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    old_ts = int(time.time()) - 7200
    _write_cache(
        cache_file,
        [{"station_id": "C4", "city": "Cached", "name": "Cached", "country": "Poland"}],
        fetched_at=old_ts,
        source_url="https://example.com/stations",
    )

    def fake_fetch_remote_stations(remote, timeout=10, token=None, token_provider=None):
        raise NoaaNetworkError("HTTP 503")

    monkeypatch.setattr(data, "fetch_remote_stations", fake_fetch_remote_stations)

    with pytest.raises(NoaaNetworkError):
        fetch_stations_with_cache_details(
            cache_path=cache_file,
            remote_url="https://example.com/stations",
            cache_ttl=0,
            stale_if_error=True,
            max_stale_seconds=60,
        )


def test_expired_cache_is_available_without_remote_source(tmp_path):
    cache_file = tmp_path / "cache.json"
    _write_cache(
        cache_file,
        [{"station_id": "C5", "city": "Cached", "name": "Cached", "country": "Poland"}],
        fetched_at=int(time.time()) - 7200,
        source_url="https://example.com/stations",
    )

    result = fetch_stations_with_cache_details(cache_path=cache_file, cache_ttl=0)

    assert result.source == "cache"
    assert result.stations[0]["station_id"] == "C5"
    assert result.metadata["cache_age_seconds"] >= 7000


def test_convenience_cache_wrapper_returns_stations(tmp_path):
    stations = fetch_stations_with_cache(cache_path=tmp_path / "missing.json")

    assert stations
    assert all("station_id" in station for station in stations)


def test_empty_remote_result_uses_sample_only_when_explicitly_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "fetch_remote_stations", lambda *args, **kwargs: ([], {}))

    result = fetch_stations_with_cache_details(
        cache_path=tmp_path / "missing.json",
        remote_url="https://example.com/stations",
        allow_sample_fallback=True,
    )

    assert result.source == "sample-fallback"
    assert result.stations == data.STATIONS
    assert result.metadata["warning"] == "Remote returned no stations"


def test_empty_remote_result_raises_without_sample_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "fetch_remote_stations", lambda *args, **kwargs: ([], {}))

    with pytest.raises(NoaaPayloadError, match="no usable stations"):
        fetch_stations_with_cache_details(
            cache_path=tmp_path / "missing.json",
            remote_url="https://example.com/stations",
        )
