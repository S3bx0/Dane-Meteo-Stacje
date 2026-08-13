from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from dane_meteo_stacje import data
from dane_meteo_stacje.countries import country_to_fips_code, normalize_country_name
from dane_meteo_stacje.data import (
    NoaaAuthError,
    NoaaNetworkError,
    TokenProvider,
    fetch_monthly_temperature_matrix,
    fetch_station_temperature_capabilities,
    fetch_stations_for_country,
    fetch_temperature_export,
)


class StationPagesClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch_json(self, url: str, token: str | None = None):
        self.urls.append(url)
        offset = int(parse_qs(urlparse(url).query)["offset"][0])
        rows = {
            1: [
                {"id": "GHCND:PL000000001", "name": "ALPHA, PL"},
                {"id": "GHCND:PL000000002", "name": "BETA, PL"},
            ],
            3: [{"id": "GHCND:PL000000003", "name": "GAMMA, PL"}],
        }.get(offset, [])
        return (
            {"metadata": {"resultset": {"count": 3}}, "results": rows},
            200,
            {"etag": "", "last_modified": ""},
        )


def test_country_search_builds_noaa_location_query_and_follows_pagination():
    client = StationPagesClient()
    result = fetch_stations_for_country(
        "Polska",
        token_provider=TokenProvider(["secret-token"]),
        page_limit=2,
        client=client,
    )

    assert result.source == "noaa-country"
    assert result.metadata["location_id"] == "FIPS:PL"
    assert result.metadata["pages"] == 2
    assert [station["station_id"] for station in result.stations] == [
        "GHCND:PL000000001",
        "GHCND:PL000000002",
        "GHCND:PL000000003",
    ]
    assert all(station["country"] == "Poland" for station in result.stations)
    assert all("locationid=FIPS%3APL" in url for url in client.urls)
    assert all(url.count("datatypeid=") == 2 for url in client.urls)


def test_country_search_uses_fresh_cache_without_a_token(tmp_path, monkeypatch):
    cache_path = tmp_path / "PL.json"
    first = fetch_stations_for_country(
        "Poland",
        token_provider=TokenProvider(["token"]),
        page_limit=2,
        client=StationPagesClient(),
        cache_path=cache_path,
    )
    for variable in ("NOAA_API_TOKENS", "NOAA_TOKENS", "NOAA_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    cached = fetch_stations_for_country("Poland", cache_path=cache_path)

    assert len(first.stations) == len(cached.stations) == 3
    assert cached.source == "noaa-country-cache"
    assert cached.metadata["cache_age_seconds"] >= 0


def test_country_search_falls_back_to_stale_cache_on_noaa_failure(tmp_path):
    cache_path = tmp_path / "PL.json"
    fetch_stations_for_country(
        "Poland",
        token_provider=TokenProvider(["token"]),
        page_limit=2,
        client=StationPagesClient(),
        cache_path=cache_path,
    )
    cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_payload["fetched_at"] = 1
    cache_path.write_text(json.dumps(cached_payload), encoding="utf-8")

    class FailingStationClient:
        def fetch_json(self, url: str, token: str | None = None):
            raise NoaaNetworkError("offline")

    result = fetch_stations_for_country(
        "Poland",
        token_provider=TokenProvider(["token"]),
        client=FailingStationClient(),
        cache_path=cache_path,
        refresh=True,
        stale_if_error=True,
    )

    assert result.source == "noaa-country-cache-stale"
    assert len(result.stations) == 3


@pytest.mark.parametrize(
    ("country", "expected_name", "expected_code"),
    [
        ("Polska", "Poland", "PL"),
        ("Germany", "Germany", "GM"),
        ("Spain", "Spain", "SP"),
        ("Czech Republic", "Czechia", "EZ"),
        ("UK", "United Kingdom", "UK"),
    ],
)
def test_country_names_use_ghcn_fips_codes(country, expected_name, expected_code):
    assert normalize_country_name(country) == expected_name
    assert country_to_fips_code(country) == expected_code


def test_country_search_requires_configured_token(monkeypatch):
    for variable in ("NOAA_API_TOKENS", "NOAA_TOKENS", "NOAA_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(NoaaAuthError, match="not configured"):
        fetch_stations_for_country("Poland")


class TemperatureClient:
    def __init__(self, timeout: float = 10, **kwargs) -> None:
        self.timeout = timeout

    def fetch_json(self, url: str, token: str | None = None):
        query = parse_qs(urlparse(url).query)
        year = int(query["startdate"][0][:4])
        results = [
            {"date": f"{year}-01-01T00:00:00", "datatype": "TMAX", "value": 10},
            {"date": f"{year}-01-02T00:00:00", "datatype": "TMAX", "value": 12},
            {"date": f"{year}-01-01T00:00:00", "datatype": "TMIN", "value": 0},
            {"date": f"{year}-01-02T00:00:00", "datatype": "TMIN", "value": 2},
        ]
        return (
            {"metadata": {"resultset": {"count": len(results)}}, "results": results},
            200,
            {"etag": "", "last_modified": ""},
        )


class TemperatureDatatypesClient:
    def fetch_json(self, url: str, token: str | None = None):
        return (
            {
                "metadata": {"resultset": {"count": 3}},
                "results": [
                    {"id": "TMIN", "name": "Minimum temperature"},
                    {"id": "TAVG", "name": "Average temperature"},
                    {"id": "TMAX", "name": "Maximum temperature"},
                ],
            },
            200,
            {"etag": "", "last_modified": ""},
        )


class ExtendedTemperatureClient(TemperatureClient):
    def fetch_json(self, url: str, token: str | None = None):
        query = parse_qs(urlparse(url).query)
        year = int(query["startdate"][0][:4])
        results = [
            {"date": f"{year}-01-01T00:00:00", "datatype": "TMIN", "value": 0, "attributes": ",,S,"},
            {"date": f"{year}-01-01T00:00:00", "datatype": "TAVG", "value": 4, "attributes": ",,S,"},
            {"date": f"{year}-01-01T00:00:00", "datatype": "TMAX", "value": 10, "attributes": ",,S,"},
            {"date": f"{year}-01-02T00:00:00", "datatype": "TMIN", "value": 2, "attributes": ",,S,"},
            {"date": f"{year}-01-02T00:00:00", "datatype": "TAVG", "value": 8, "attributes": ",,S,"},
            {"date": f"{year}-01-02T00:00:00", "datatype": "TMAX", "value": 12, "attributes": ",,S,"},
            {"date": f"{year}-01-03T00:00:00", "datatype": "TAVG", "value": 99, "attributes": ",X,S,"},
        ]
        return (
            {"metadata": {"resultset": {"count": len(results)}}, "results": results},
            200,
            {"etag": "", "last_modified": ""},
        )


def test_station_temperature_capabilities_expose_reported_and_derived_methods():
    payload = fetch_station_temperature_capabilities(
        "GHCND:PLM00012295",
        token_provider=TokenProvider(["token"]),
        client=TemperatureDatatypesClient(),
    )

    assert payload["core_temperature_datatypes"] == ["TMIN", "TAVG", "TMAX"]
    assert payload["derived_datatypes"] == {"TAXN": True, "AMPLITUDE": True}
    assert payload["export_modes"] == {
        "heatmap": True,
        "daily": True,
        "monthly": True,
        "extended": True,
    }
    assert payload["temperature_methods"]["TAVG"]["source_dependent"] is True
    assert payload["temperature_methods"]["TAXN"]["formula"] == "(TMAX + TMIN) / 2"


def test_temperature_export_matches_heatmap_matrix(monkeypatch):
    monkeypatch.setattr(data, "NoaaClient", TemperatureClient)
    payload = fetch_monthly_temperature_matrix(
        "GHCND:PLM00012295",
        2020,
        2021,
        token_provider=TokenProvider(["token-a", "token-b"]),
        concurrency=2,
        max_attempts=1,
    )

    assert list(payload) == [
        "station_id",
        "years",
        "months",
        "temperatures",
        "final_missing_years",
        "missing_data_report",
        "token_usage",
        "adaptive_history",
    ]
    assert payload["station_id"] == "PLM00012295"
    assert payload["years"] == [2020, 2021]
    assert payload["months"] == [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    assert payload["temperatures"][0] == [6.0] + [None] * 11
    assert payload["temperatures"][1] == [6.0] + [None] * 11
    assert payload["final_missing_years"] == []
    assert sum(payload["token_usage"].values()) == 2


def test_daily_temperature_export_keeps_tavg_separate_from_taxn(monkeypatch):
    monkeypatch.setattr(data, "NoaaClient", ExtendedTemperatureClient)
    payload = fetch_temperature_export(
        "PLM00012295",
        2024,
        2024,
        mode="daily",
        token_provider=TokenProvider(["token"]),
        max_attempts=1,
    )

    assert payload["export_type"] == "daily"
    assert payload["temperature_methods"]["TAVG"]["source_dependent"] is True
    assert payload["temperature_methods"]["TAXN"]["formula"] == "(TMAX + TMIN) / 2"
    assert payload["aggregation_methods"]["monthly_mean"] == (
        "arithmetic_mean_of_available_quality_controlled_daily_values"
    )
    assert payload["data"][0] == {
        "date": "2024-01-01",
        "tmin": 0.0,
        "tavg": 4.0,
        "taxn": 5.0,
        "tmax": 10.0,
        "amplitude": 10.0,
        "attributes": {
            "TMIN": {
                "measurement_flag": "",
                "quality_flag": "",
                "source_flag": "S",
                "observation_time": "",
            },
            "TAVG": {
                "measurement_flag": "",
                "quality_flag": "",
                "source_flag": "S",
                "observation_time": "",
            },
            "TMAX": {
                "measurement_flag": "",
                "quality_flag": "",
                "source_flag": "S",
                "observation_time": "",
            },
        },
    }
    assert payload["quality_control"]["records_rejected_quality"] == 1
    assert payload["completeness"]["taxn"]["observed_days"] == 2


def test_monthly_temperature_export_contains_separate_matrices_and_completeness(monkeypatch):
    monkeypatch.setattr(data, "NoaaClient", ExtendedTemperatureClient)
    payload = fetch_temperature_export(
        "PLM00012295",
        2024,
        2024,
        mode="monthly",
        token_provider=TokenProvider(["token"]),
        max_attempts=1,
    )

    assert payload["temperatures"]["TMIN"][0][0] == 1.0
    assert payload["temperatures"]["TAVG"][0][0] == 6.0
    assert payload["temperatures"]["TAXN"][0][0] == 6.0
    assert payload["temperatures"]["TMAX"][0][0] == 11.0
    assert payload["temperatures"]["AMPLITUDE"][0][0] == 10.0
    assert payload["completeness"]["expected_days"][0][0] == 31
    assert payload["completeness"]["percent"]["TAVG"][0][0] == 6.45


def test_extended_temperature_export_compares_reported_tavg_with_taxn(monkeypatch):
    monkeypatch.setattr(data, "NoaaClient", ExtendedTemperatureClient)
    payload = fetch_temperature_export(
        "PLM00012295",
        2024,
        2024,
        mode="extended",
        token_provider=TokenProvider(["token"]),
        max_attempts=1,
    )

    january = payload["monthly_statistics"][0]
    assert january["temperatures"]["AMPLITUDE"]["mean"] == 10.0
    assert january["temperatures"]["TAVG"]["completeness_percent"] == 6.45
    assert january["tavg_taxn_comparison"] == {
        "paired_days": 2,
        "mean_difference": 0.0,
        "mean_absolute_difference": 1.0,
        "maximum_absolute_difference": 1.0,
    }


def test_temperature_quality_flagged_observations_are_ignored():
    monthly = data._monthly_temperatures_from_records(
        2024,
        [
            {"date": "2024-01-01", "datatype": "TMAX", "value": 100, "attributes": ",X,S,"},
            {"date": "2024-01-02", "datatype": "TMAX", "value": 10, "attributes": ",,S,"},
            {"date": "2024-01-02", "datatype": "TMIN", "value": 0, "attributes": ",,S,"},
        ],
    )

    assert monthly == [5.0] + [None] * 11


def test_temperature_total_remote_failure_raises_instead_of_returning_null_matrix(monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            pass

        def fetch_json(self, url: str, token: str | None = None):
            raise NoaaNetworkError("offline")

    monkeypatch.setattr(data, "NoaaClient", FailingClient)

    with pytest.raises(NoaaNetworkError, match="offline"):
        fetch_monthly_temperature_matrix(
            "PLM00012295",
            2020,
            2020,
            token_provider=TokenProvider(["token-a", "token-b"]),
            max_attempts=1,
        )


def test_temperature_year_cache_avoids_duplicate_noaa_requests(monkeypatch, tmp_path):
    calls = []

    class CountingClient(TemperatureClient):
        def __init__(self, **kwargs):
            super().__init__(timeout=float(kwargs.get("timeout", 10)))

        def fetch_json(self, url: str, token: str | None = None):
            calls.append(url)
            return super().fetch_json(url, token)

    monkeypatch.setattr(data, "NoaaClient", CountingClient)
    first = fetch_monthly_temperature_matrix(
        "PLM00012295",
        2020,
        2020,
        token_provider=TokenProvider(["token"]),
        cache_dir=tmp_path,
        max_attempts=1,
    )
    call_count = len(calls)
    second = fetch_monthly_temperature_matrix(
        "PLM00012295",
        2020,
        2020,
        cache_dir=tmp_path,
    )

    assert first["temperatures"] == second["temperatures"]
    assert len(calls) == call_count
    assert second["token_usage"] == {}


def test_temperature_rejects_future_year():
    future_year = time.gmtime().tm_year + 1
    with pytest.raises(ValueError, match="year range"):
        fetch_monthly_temperature_matrix(
            "PLM00012295",
            future_year,
            future_year,
            token_provider=TokenProvider(["token"]),
        )
