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
    page.get_by_label("Query (optional)").fill("Warsaw")
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


def test_server_busy_is_rendered(page: Any, gui_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted_limiter = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    for _ in range(gui.MAX_CONCURRENT_FETCHES):
        assert exhausted_limiter.acquire(blocking=False)
    monkeypatch.setattr(gui, "_FETCH_LIMITER", exhausted_limiter)

    page.goto(gui_url)
    page.get_by_role("button", name="Search").click()

    expect(page.locator("#status-badge")).to_have_text("Error")
    expect(page.locator("#message")).to_contain_text("SERVER_BUSY")