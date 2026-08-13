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
    expect(page.locator("#map-summary")).to_contain_text("wszystkie 1")
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "9")
    page.get_by_role("button", name="Pokaż świat").click()
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "2")
    page.get_by_role("button", name="Dopasuj stacje").click()
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "9")
    page.get_by_role("button", name="Select").click()
    expect(page.locator("#map-selected-station")).to_contain_text("Warsaw Station")
    expect(page.locator("#map-selected-station")).to_contain_text("52.2300")
    expect(page.locator("#station-map .station-map-dot.selected")).to_have_count(1)
    expect(page.locator("#station-map")).to_have_attribute("data-zoom", "11")
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
    monkeypatch.setattr(gui, "fetch_temperature_export", lambda *args, **kwargs: daily)

    page.goto(gui_url)
    page.get_by_label("Country (NOAA)").fill("Poland")
    page.get_by_role("button", name="Search").click()
    page.get_by_role("button", name="Select").click()
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


def test_server_busy_is_rendered(page: Any, gui_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted_limiter = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    for _ in range(gui.MAX_CONCURRENT_FETCHES):
        assert exhausted_limiter.acquire(blocking=False)
    monkeypatch.setattr(gui, "_FETCH_LIMITER", exhausted_limiter)

    page.goto(gui_url)
    page.get_by_role("button", name="Search").click()

    expect(page.locator("#status-badge")).to_have_text("Error")
    expect(page.locator("#message")).to_contain_text("SERVER_BUSY")
