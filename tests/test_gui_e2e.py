import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

import dane_meteo_stacje.gui_bootstrap as gui
from dane_meteo_stacje.data import FetchResult, NoaaNetworkError

pytestmark = pytest.mark.e2e
playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect


@pytest.fixture(autouse=True)
def country_boundary_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[14.1, 54.8], [24.1, 54.8], [24.1, 49.0], [14.1, 54.8]]],
    }
    monkeypatch.setattr(
        gui,
        "fetch_country_boundary",
        lambda country: (
            {
                "type": "Feature",
                "properties": {"country": country, "attribution": "© OpenStreetMap contributors"},
                "geometry": geometry,
            },
            "cache",
        ),
    )


@pytest.fixture
def gui_url() -> Iterator[str]:
    server = gui.AppHTTPServer(("127.0.0.1", 0), gui.AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _station() -> dict[str, object]:
    return {
        "station_id": "PL000012345",
        "city": "Warsaw",
        "name": "Warsaw Station",
        "country": "Poland",
        "latitude": 52.23,
        "longitude": 21.01,
        "mindate": "1980-01-01",
        "maxdate": "2025-12-31",
        "datacoverage": 0.95,
    }


def _monthly_preview(station_id: str = "PL000012345", offset: float = 0.0) -> dict[str, object]:
    years = [2023, 2024, 2025]
    expected_days = [[31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] for _ in years]
    values = [
        [float(month + year_index) + offset for month in range(1, 13)]
        for year_index, _ in enumerate(years)
    ]
    completeness = [[100.0] * 12 for _ in years]
    completeness[1] = [80.0] * 12
    return {
        "schema_version": 1,
        "export_type": "monthly",
        "station_id": station_id,
        "period": {"start_date": "2023-01-01", "end_date": "2025-12-31"},
        "years": years,
        "months": [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        "observed_datatypes": ["TMIN", "TAVG", "TMAX"],
        "temperatures": {
            "TMIN": [[value - 3 for value in row] for row in values],
            "TAVG": values,
            "TAXN": values,
            "TMAX": [[value + 3 for value in row] for row in values],
            "AMPLITUDE": [[6.0] * 12 for _ in years],
        },
        "completeness": {
            "expected_days": expected_days,
            "percent": {
                "TMIN": completeness,
                "TAVG": completeness,
                "TAXN": completeness,
                "TMAX": completeness,
                "AMPLITUDE": completeness,
            },
        },
    }


def test_search_and_export_flow(page: Any, gui_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gui,
        "fetch_stations_with_cache_details",
        lambda **kwargs: FetchResult(stations=[_station()], source="remote", metadata={}),
    )

    page.goto(gui_url)
    page.get_by_label("Name or city filter (optional)").fill("Warsaw")
    page.get_by_role("button", name="Search").click()

    expect(page.locator("#status-badge")).to_have_text("OK")
    expect(page.locator("#message")).to_contain_text("Found 1 station(s). Source: remote.")
    expect(page.locator("#result-body tr")).to_have_count(1)
    expect(page.locator("#result-body")).to_contain_text("PL000012345")

    with page.expect_download() as json_download_info:
        page.get_by_role("button", name="Export JSON").click()
    json_download = json_download_info.value
    assert json_download.suggested_filename == "stations.json"
    assert json.loads(json_download.path().read_text(encoding="utf-8"))[0]["station_id"] == "PL000012345"

    with page.expect_download() as csv_download_info:
        page.get_by_role("button", name="Export CSV").click()
    csv_download = csv_download_info.value
    assert csv_download.suggested_filename == "stations.csv"
    assert "PL000012345,Warsaw" in csv_download.path().read_text(encoding="utf-8")


def test_noaa_error_is_rendered(page: Any, gui_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_fetch(**kwargs: object) -> FetchResult:
        raise NoaaNetworkError("offline")

    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", fail_fetch)

    page.goto(gui_url)
    page.get_by_role("button", name="Search").click()

    expect(page.locator("#status-badge")).to_have_text("Error")
    expect(page.locator("#message")).to_contain_text("NOAA_NETWORK")
    expect(page.locator("#message")).not_to_contain_text("offline")


def test_country_only_search_selects_station_and_downloads_heatmap_json(
    page: Any,
    gui_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gui,
        "fetch_stations_for_country",
        lambda *args, **kwargs: FetchResult(
            stations=[_station()],
            source="noaa-country",
            metadata={"country": "Poland"},
        ),
    )
    heatmap = {
        "station_id": "PL000012345",
        "years": [2024],
        "months": [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        "temperatures": [[1.0] + [None] * 11],
        "final_missing_years": [],
        "missing_data_report": {},
        "token_usage": {},
        "adaptive_history": [],
    }
    monkeypatch.setattr(gui, "fetch_monthly_temperature_matrix", lambda *args, **kwargs: heatmap)
    monkeypatch.setattr(gui, "fetch_temperature_export", lambda *args, **kwargs: _monthly_preview())
    monkeypatch.setattr(
        gui,
        "fetch_station_temperature_capabilities",
        lambda *args, **kwargs: {
            "station_id": "PL000012345",
            "dataset_id": "GHCND",
            "available_datatypes": ["TMIN", "TMAX"],
            "core_temperature_datatypes": ["TMIN", "TMAX"],
            "datatype_details": [],
            "derived_datatypes": {"TAXN": True, "AMPLITUDE": True},
            "export_modes": {"heatmap": True, "daily": True, "monthly": True, "extended": True},
            "temperature_methods": {},
        },
    )

    page.goto(gui_url)
    expect(page.locator("#station-map")).to_have_attribute("data-map-ready", "true")
    page.get_by_label("Country (NOAA)").fill("Poland")
    page.get_by_role("button", name="Search").click()
    expect(page.locator("#result-body tr")).to_have_count(1)
    expect(page.locator("#station-map .station-map-dot")).to_have_count(1)
    expect(page.locator("#station-map canvas")).to_have_count(1)
    expect(page.locator("#map-country-boundary")).to_contain_text("Poland")
    expect(page.locator("#map-summary")).to_contain_text("wszystkie 1")
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "9")
    page.get_by_role("button", name="Pokaż świat").click()
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "2")
    page.get_by_role("button", name="Dopasuj stacje").click()
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "9")
    page.get_by_role("button", name="Minimum 30 lat / 90%").click()
    expect(page.locator("#quality-filter-summary")).to_contain_text("Widoczne 1 z 1")
    expect(page.locator("#station-map .station-map-dot.quality-good")).to_have_count(1)
    page.get_by_role("button", name="Wybierz najlepszą stację").click()
    expect(page.locator("#map-selected-station")).to_contain_text("Warsaw Station")
    expect(page.locator("#map-selected-station")).to_contain_text("52.2300")
    expect(page.locator("#station-map .station-map-dot.selected")).to_have_count(1)
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "11")
    expect(page.locator("#preview-status")).to_contain_text("Podgląd 2023-01-01–2025-12-31")
    expect(page.locator("#temperature-preview-chart path")).to_have_count(3)
    expect(page.locator("#amplitude-preview-chart rect")).to_have_count(36)
    expect(page.locator("#preview-incomplete-years")).to_have_text("1")
    page.get_by_label("Start year").fill("2024")
    page.get_by_label("End year").fill("2024")

    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download temperature JSON").click()

    download = download_info.value
    assert download.suggested_filename == "warsaw_temperatures.json"
    assert json.loads(download.path().read_text(encoding="utf-8"))["station_id"] == "PL000012345"


def test_daily_export_checks_capabilities_and_uses_separate_filename(
    page: Any,
    gui_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gui,
        "fetch_stations_for_country",
        lambda *args, **kwargs: FetchResult(
            stations=[_station()],
            source="noaa-country",
            metadata={"country": "Poland"},
        ),
    )
    monkeypatch.setattr(
        gui,
        "fetch_station_temperature_capabilities",
        lambda *args, **kwargs: {
            "station_id": "PL000012345",
            "dataset_id": "GHCND",
            "available_datatypes": ["TMIN", "TAVG", "TMAX"],
            "core_temperature_datatypes": ["TMIN", "TAVG", "TMAX"],
            "datatype_details": [],
            "derived_datatypes": {"TAXN": True, "AMPLITUDE": True},
            "export_modes": {"heatmap": True, "daily": True, "monthly": True, "extended": True},
            "temperature_methods": {},
        },
    )
    daily = {
        "schema_version": 1,
        "export_type": "daily",
        "station_id": "PL000012345",
        "period": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "temperature_methods": {"TAXN": {"formula": "(TMAX + TMIN) / 2"}},
        "data": [],
    }
    monkeypatch.setattr(
        gui,
        "fetch_temperature_export",
        lambda *args, **kwargs: _monthly_preview() if kwargs.get("mode") == "monthly" else daily,
    )

    page.goto(gui_url)
    page.get_by_label("Country (NOAA)").fill("Poland")
    page.get_by_role("button", name="Search").click()
    page.get_by_role("button", name="Wybierz", exact=True).first.click()
    expect(page.locator("#temperature-capabilities")).to_contain_text("TAVG")
    page.get_by_label("Rodzaj eksportu").select_option("daily")
    page.get_by_label("Start year").fill("2024")
    page.get_by_label("End year").fill("2024")

    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download temperature JSON").click()

    download = download_info.value
    assert download.suggested_filename == "warsaw_daily_temperatures.json"
    exported = json.loads(download.path().read_text(encoding="utf-8"))
    assert exported["temperature_methods"]["TAXN"]["formula"] == "(TMAX + TMIN) / 2"


def test_comparison_of_two_stations_shows_common_chart_and_distance_matrix(
    page: Any,
    gui_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_station = {
        "station_id": "PL000054321",
        "city": "Krakow",
        "name": "Krakow Station",
        "country": "Poland",
        "latitude": 50.06,
        "longitude": 19.94,
        "mindate": "1990-01-01",
        "maxdate": "2025-12-31",
        "datacoverage": 0.92,
    }
    monkeypatch.setattr(
        gui,
        "fetch_stations_for_country",
        lambda *args, **kwargs: FetchResult(
            stations=[_station(), second_station],
            source="noaa-country",
            metadata={"country": "Poland"},
        ),
    )

    def fake_export(station_id: str, *args: object, **kwargs: object) -> dict[str, object]:
        return _monthly_preview(station_id, 2.5 if station_id == "PL000054321" else 0.0)

    monkeypatch.setattr(gui, "fetch_temperature_export", fake_export)

    page.goto(gui_url)
    page.get_by_label("Country (NOAA)").fill("Poland")
    page.get_by_role("button", name="Search").click()
    expect(page.locator("#result-body tr")).to_have_count(2)
    page.get_by_role("button", name="Porównaj", exact=True).first.click()
    page.get_by_role("button", name="Porównaj", exact=True).first.click()

    expect(page.locator("#comparison-count")).to_have_text("2/5")
    expect(page.locator("#comparison-status")).to_contain_text("Porównano 2 stacji")
    expect(page.locator("#comparison-temperature-chart path")).to_have_count(2)
    expect(page.locator("#comparison-summary-body tr")).to_have_count(2)
    expect(page.locator("#comparison-distance-body tr")).to_have_count(2)
    expect(page.locator("#comparison-temperature-spread")).to_have_text("2.50°C")
    expect(page.locator("#comparison-summary-body")).to_contain_text("Krakow Station")


def test_server_busy_is_rendered(page: Any, gui_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted_limiter = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    for _ in range(gui.MAX_CONCURRENT_FETCHES):
        assert exhausted_limiter.acquire(blocking=False)
    monkeypatch.setattr(gui, "_FETCH_LIMITER", exhausted_limiter)

    page.goto(gui_url)
    page.get_by_role("button", name="Search").click()

    expect(page.locator("#status-badge")).to_have_text("Error")
    expect(page.locator("#message")).to_contain_text("SERVER_BUSY")
