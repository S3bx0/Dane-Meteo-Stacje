import http.client
import io
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

import dane_meteo_stacje.gui_bootstrap as gui
from dane_meteo_stacje.data import FetchResult, NoaaNetworkError
from dane_meteo_stacje.observability import configure_logging, log_event


@pytest.fixture
def gui_server() -> Iterator[tuple[str, int]]:
    server = gui.ThreadingHTTPServer(("127.0.0.1", 0), gui.AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _request(
    address: tuple[str, int],
    method: str,
    path: str,
    payload: Any | None = None,
    *,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = raw_body if raw_body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = http.client.HTTPConnection(*address)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, response_body


def _station(station_id: str, city: str, country: str = "Poland") -> dict[str, Any]:
    return {
        "station_id": station_id,
        "city": city,
        "name": f"{city} Station",
        "country": country,
        "latitude": 52.0,
        "longitude": 21.0,
    }


def test_get_routes(gui_server):
    status, headers, body = _request(gui_server, "GET", "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert b"Dane Meteo Stacje" in body

    status, headers, body = _request(gui_server, "GET", "/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is True
    assert len(payload["request_id"]) == 32
    assert headers["x-request-id"] == payload["request_id"]

    status, _, body = _request(gui_server, "GET", "/missing")
    assert status == 404
    assert json.loads(body)["code"] == "NOT_FOUND"


def test_post_rejects_unknown_route_and_invalid_json(gui_server):
    status, _, body = _request(gui_server, "POST", "/missing", {})
    assert status == 404
    assert json.loads(body)["code"] == "NOT_FOUND"

    status, _, body = _request(gui_server, "POST", "/api/search", raw_body=b"not-json")
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"

    status, _, body = _request(gui_server, "POST", "/api/search", [])
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_country_only_search_filters_sorts_and_limits(gui_server, monkeypatch):
    stations = [
        _station("PL2", "Warsaw"),
        _station("DE1", "Berlin", "Germany"),
        _station("PL1", "Gdansk"),
    ]
    monkeypatch.setattr(
        gui,
        "fetch_stations_with_cache_details",
        lambda **kwargs: FetchResult(stations=stations, source="remote", metadata={"http_status": 200}),
    )

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/search",
        {"country": "Poland", "sort": "station_id", "limit": 1},
    )
    payload = json.loads(body)

    assert status == 200
    assert payload["source"] == "remote"
    assert [row["station_id"] for row in payload["results"]] == ["PL1"]


def test_query_search_applies_country_and_station_filters(gui_server, monkeypatch):
    stations = [_station("PL1", "Warsaw"), _station("PL2", "Gdansk")]
    monkeypatch.setattr(
        gui,
        "fetch_stations_with_cache_details",
        lambda **kwargs: FetchResult(stations=stations, source="cache-fresh", metadata={}),
    )

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/search",
        {"query": "station", "country": "Poland", "station_id": "PL2", "sort": "name"},
    )

    assert status == 200
    assert [row["station_id"] for row in json.loads(body)["results"]] == ["PL2"]


def test_search_validates_numeric_options(gui_server):
    status, _, body = _request(gui_server, "POST", "/api/search", {"limit": 0})
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_search_maps_noaa_errors(gui_server, monkeypatch):
    def fail_fetch(**kwargs):
        raise NoaaNetworkError("offline")

    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", fail_fetch)
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/search",
        {"remote_url": "https://www.ncei.noaa.gov/cdo-web/api/v2/stations"},
    )

    assert status == 502
    assert json.loads(body)["code"] == "NOAA_NETWORK"


def test_unexpected_error_returns_safe_correlated_500_and_logs_traceback(gui_server, monkeypatch):
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    request_completed = threading.Event()
    real_log_event = gui.log_event

    def capture_log_event(event, **fields):
        real_log_event(event, **fields)
        if event == "http_request_completed":
            request_completed.set()

    def fail_fetch(**kwargs):
        raise RuntimeError("internal diagnostic detail")

    monkeypatch.setattr(gui, "log_event", capture_log_event)
    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", fail_fetch)
    status, headers, body = _request(gui_server, "POST", "/api/search", {})
    payload = json.loads(body)
    assert request_completed.wait(timeout=1)

    assert status == 500
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["message"] == "Internal server error"
    assert "internal diagnostic detail" not in body.decode("utf-8")
    assert headers["x-request-id"] == payload["request_id"]

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed_event = next(event for event in events if event["event"] == "http_request_failed")
    completed_event = next(event for event in events if event["event"] == "http_request_completed")
    assert failed_event["request_id"] == payload["request_id"]
    assert failed_event["error_type"] == "RuntimeError"
    assert completed_event["request_id"] == payload["request_id"]
    assert completed_event["duration_ms"] >= 0
    assert "Traceback (most recent call last)" in failed_event["traceback"]
    assert "internal diagnostic detail" in failed_event["traceback"]


def test_client_disconnect_is_logged_and_request_context_is_reset(monkeypatch):
    events = []
    handler = object.__new__(gui.AppHandler)
    handler.path = "/health"
    monkeypatch.setattr(gui, "log_event", lambda event, **fields: events.append((event, fields)))

    handler._run_request("GET", lambda: (_ for _ in ()).throw(BrokenPipeError()))

    assert [event for event, _ in events] == [
        "http_request_started",
        "http_client_disconnected",
        "http_request_completed",
    ]
    assert events[1][1]["level"] == gui.logging.WARNING
    assert events[2][1]["duration_ms"] >= 0

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    log_event("after_disconnected_request")
    assert "request_id" not in json.loads(stream.getvalue())


def test_export_json_normalizes_rows(gui_server):
    rows = [
        {
            **_station("PL1", "Warsaw"),
            "latitude": "invalid",
            "longitude": "21.5",
            "source": "NOAA",
            "notes": "active",
        },
        {"station_id": "invalid"},
        "not-an-object",
    ]
    status, headers, body = _request(gui_server, "POST", "/api/export", {"format": "json", "rows": rows})
    payload = json.loads(body)

    assert status == 200
    assert headers["content-disposition"] == 'attachment; filename="stations.json"'
    assert len(payload) == 1
    assert "latitude" not in payload[0]
    assert payload[0]["longitude"] == 21.5
    assert payload[0]["source"] == "NOAA"


def test_export_csv_and_validation(gui_server):
    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/export",
        {"format": "csv", "rows": [_station("PL1", "Warsaw")]},
    )
    assert status == 200
    assert headers["content-disposition"] == 'attachment; filename="stations.csv"'
    assert b"station_id,city,name,country" in body
    assert b"PL1,Warsaw" in body

    status, _, body = _request(gui_server, "POST", "/api/export", {"format": "xml", "rows": []})
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"

    status, _, body = _request(gui_server, "POST", "/api/export", {"format": "json", "rows": "invalid"})
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_parse_int_defaults_and_rejects_too_small_values():
    assert gui._parse_int(None, default=7) == 7
    assert gui._parse_int("8", minimum=1) == 8
    with pytest.raises(ValueError):
        gui._parse_int("0", minimum=1)


def test_main_forwards_server_options(monkeypatch):
    calls = []
    monkeypatch.setattr(gui, "run_server", lambda host, port, *, open_browser: calls.append((host, port, open_browser)))

    assert gui.main(["--host", "127.0.0.2", "--port", "9999", "--no-browser"]) == 0
    assert calls == [("127.0.0.2", 9999, False)]
