import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_search_by_city_name_returns_station_matches():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "Bialystok"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "bialystok" in result.stdout.lower()
    assert "PLM00012295" in result.stdout


def test_info_for_known_station_shows_metadata():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "info", "PLM00012295"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Bialystok" in result.stdout
    assert "PLM00012295" in result.stdout


def test_search_can_output_json():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "Bialystok", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert '"station_id": "PLM00012295"' in result.stdout


def test_search_can_load_station_data_from_json_file(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {
                    "station_id": "XYZ123456",
                    "city": "Berlin",
                    "name": "Berlin",
                    "country": "Germany",
                    "source": "custom-json",
                    "notes": "loaded from a file",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "Berlin", "--source", str(custom_source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Berlin" in result.stdout
    assert "XYZ123456" in result.stdout


def test_export_to_json_and_csv(tmp_path):
    output_json = tmp_path / "stations.json"
    output_csv = tmp_path / "stations.csv"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "export",
            "--output-json",
            str(output_json),
            "--output-csv",
            str(output_csv),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert output_json.exists()
    assert output_csv.exists()
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload
    with output_csv.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows


def test_network_failures_use_cache_when_available(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland"}]), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "search",
            "cache",
            "--cache",
            str(cache_file),
            "--remote-url",
            "https://example.invalid/stations.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Cache City" in result.stdout


def test_expired_cache_is_ignored_when_ttl_elapsed(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland"}]), encoding="utf-8")
    old_time = time.time() - 7200
    os.utime(cache_file, (old_time, old_time))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "search",
            "cache",
            "--cache",
            str(cache_file),
            "--remote-url",
            "https://example.invalid/stations.json",
            "--cache-ttl",
            "0",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Cache City" not in result.stdout


def test_refresh_flag_forces_remote_fetch_when_cache_exists(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland"}]), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "search",
            "cache",
            "--cache",
            str(cache_file),
            "--remote-url",
            "https://example.invalid/stations.json",
            "--refresh",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Cache City" not in result.stdout


def test_noaa_payload_is_mapped_to_station_dict():
    from dane_meteo_stacje.data import _normalize_noaa_payload

    payload = {
        "results": [
            {
                "id": "USW00014898",
                "name": "BALTIMORE WASHINGTON INTL AP",
                "latitude": 39.17,
                "longitude": -76.68,
                "elevation": 47.0,
                "mindate": "1930-01-01",
                "maxdate": "2025-12-31",
                "datatype": ["TMAX", "TMIN"],
                "datacoverage": 1.0,
                "id": "USW00014898",
            }
        ]
    }

    stations = _normalize_noaa_payload(payload)
    assert stations[0]["station_id"] == "USW00014898"
    assert stations[0]["city"] == "Baltimore"
    assert stations[0]["name"] == "BALTIMORE WASHINGTON INTL AP"


def test_cache_metadata_command_shows_timestamp_and_count(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland"}]), encoding="utf-8")
    metadata_path = tmp_path / "cache.json.meta.json"
    metadata_path.write_text(json.dumps({"timestamp": 1234567890, "count": 1}), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "cache-meta", str(cache_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "1234567890" in result.stdout
    assert "1" in result.stdout


def test_search_results_include_source_hint_when_requested(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland", "source": "cache"}]), encoding="utf-8")
    metadata_path = tmp_path / "cache.json.meta.json"
    metadata_path.write_text(json.dumps({"timestamp": 1234567890, "count": 1}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "search",
            "cache",
            "--cache",
            str(cache_file),
            "--show-source",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "cache" in result.stdout.lower()
    assert "1234567890" in result.stdout


def test_search_limit_limits_results(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {"station_id": "A1", "city": "Alpha", "name": "Test Alpha", "country": "Poland"},
                {"station_id": "B2", "city": "Beta", "name": "Test Beta", "country": "Poland"},
                {"station_id": "C3", "city": "Gamma", "name": "Test Gamma", "country": "Poland"},
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "test", "--source", str(custom_source), "--limit", "2"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Test Alpha" in result.stdout
    assert "Test Beta" in result.stdout
    assert "Test Gamma" not in result.stdout


def test_search_can_filter_by_country(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {"station_id": "PL1", "city": "Warsaw", "name": "Test Warsaw", "country": "Poland"},
                {"station_id": "DE1", "city": "Berlin", "name": "Test Berlin", "country": "Germany"},
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "test", "--source", str(custom_source), "--country", "Poland"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Warsaw" in result.stdout
    assert "Berlin" not in result.stdout


def test_search_results_are_sorted_by_city(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {"station_id": "Z1", "city": "Zulu", "name": "Test Zulu", "country": "Poland"},
                {"station_id": "A1", "city": "Alpha", "name": "Test Alpha", "country": "Poland"},
                {"station_id": "M1", "city": "Mike", "name": "Test Mike", "country": "Poland"},
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "test", "--source", str(custom_source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.index("Alpha") < result.stdout.index("Mike") < result.stdout.index("Zulu")


def test_show_source_output_is_more_readable(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps([{"station_id": "CACHE1", "city": "Cache City", "name": "Cache City", "country": "Poland", "source": "cache"}]), encoding="utf-8")
    metadata_path = tmp_path / "cache.json.meta.json"
    metadata_path.write_text(json.dumps({"timestamp": 1234567890, "count": 1}), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "cache", "--cache", str(cache_file), "--show-source"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "source: cache" in result.stdout
    assert "cache timestamp: 1234567890" in result.stdout


def test_search_can_filter_by_station_id(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {"station_id": "PL1", "city": "Warsaw", "name": "Test Warsaw", "country": "Poland"},
                {"station_id": "DE1", "city": "Berlin", "name": "Test Berlin", "country": "Germany"},
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "test", "--source", str(custom_source), "--station-id", "DE1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Berlin" in result.stdout
    assert "Warsaw" not in result.stdout


def test_export_can_write_subset_to_json(tmp_path):
    custom_source = tmp_path / "stations.json"
    custom_source.write_text(
        json.dumps(
            [
                {"station_id": "PL1", "city": "Warsaw", "name": "Test Warsaw", "country": "Poland"},
                {"station_id": "DE1", "city": "Berlin", "name": "Test Berlin", "country": "Germany"},
            ]
        ),
        encoding="utf-8",
    )
    output_json = tmp_path / "subset.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "export",
            "--source",
            str(custom_source),
            "--output-json",
            str(output_json),
            "--station-id",
            "PL1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["station_id"] == "PL1"


def test_noaa_payload_with_state_and_region_is_mapped():
    from dane_meteo_stacje.data import _normalize_noaa_payload

    payload = {
        "results": [
            {
                "id": "USW00099999",
                "name": "Station in Texas",
                "location": {"state": "TX", "region": "Texas"},
                "country": "USA",
            }
        ]
    }

    stations = _normalize_noaa_payload(payload)
    assert stations[0]["city"] == "Texas"


def test_noaa_payload_with_nested_fields_is_mapped():
    from dane_meteo_stacje.data import _normalize_noaa_payload

    payload = {
        "metadata": {"resultset": {"count": 1}},
        "results": [
            {
                "id": "USW00013739",
                "name": "NEWARK INTERNATIONAL AIRPORT",
                "location": {"city": "Newark", "state": "NJ"},
                "coordinates": {"latitude": 40.7, "longitude": -74.17},
            }
        ],
    }

    stations = _normalize_noaa_payload(payload)
    assert stations[0]["city"] == "Newark"
    assert stations[0]["station_id"] == "USW00013739"


def test_pretty_flag_formats_output_for_human_readability(tmp_path):
    output_json = tmp_path / "stations.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dane_meteo_stacje",
            "export",
            "--output-json",
            str(output_json),
            "--pretty",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    data = json.loads(output_json.read_text(encoding="utf-8"))
    assert data
