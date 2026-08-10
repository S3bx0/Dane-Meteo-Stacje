import http.client
import io
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import dane_meteo_stacje.gui_bootstrap as gui
from dane_meteo_stacje.data import FetchResult, NoaaNetworkError
from dane_meteo_stacje.observability import configure_logging, log_event


@pytest.fixture
def gui_server() -> Iterator[tuple[str, int]]:
    server = gui.AppHTTPServer(("127.0.0.1", 0), gui.AppHandler)
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
    connection = http.client.HTTPConnection(*address, timeout=2)
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


def _assert_json_contract(
    headers: dict[str, str],
    body: bytes,
    *,
    error_code: str | None = None,
) -> dict[str, Any]:
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    payload = json.loads(body)
    assert isinstance(payload, dict)
    assert len(payload["request_id"]) == 32
    assert headers["x-request-id"] == payload["request_id"]
    if error_code is not None:
        assert payload["code"] == error_code
        assert isinstance(payload["message"], str)
        assert payload["message"]
    return payload


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


def test_endpoint_json_contracts(gui_server, monkeypatch):
    monkeypatch.setattr(
        gui,
        "fetch_stations_with_cache_details",
        lambda **kwargs: FetchResult(stations=[_station("PL1", "Warsaw")], source="remote", metadata={}),
    )

    cases = [
        ("GET", "/health", None, None),
        ("GET", "/missing", None, "NOT_FOUND"),
        ("POST", "/missing", {}, "NOT_FOUND"),
        ("POST", "/api/search", {"query": "Warsaw"}, None),
        ("POST", "/api/search", {"limit": 0}, "BAD_REQUEST"),
        ("POST", "/api/export", {"format": "xml", "rows": []}, "BAD_REQUEST"),
    ]

    for method, path, request_payload, error_code in cases:
        status, headers, body = _request(gui_server, method, path, request_payload)
        payload = _assert_json_contract(headers, body, error_code=error_code)
        assert status == (200 if error_code is None else 400 if error_code == "BAD_REQUEST" else 404)
        if path == "/health":
            assert payload["ok"] is True
        elif path == "/api/search" and error_code is None:
            assert payload["source"] == "remote"
            assert isinstance(payload["results"], list)
            assert isinstance(payload["metadata"], dict)


def test_concurrent_health_requests_have_unique_correlated_request_ids(gui_server):
    with ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(lambda _: _request(gui_server, "GET", "/health"), range(20)))

    request_ids = []
    for status, headers, body in responses:
        payload = json.loads(body)
        assert status == 200
        assert payload["ok"] is True
        assert headers["x-request-id"] == payload["request_id"]
        request_ids.append(payload["request_id"])

    assert len(set(request_ids)) == 20


def test_fetch_concurrency_limit_returns_503_without_blocking_health(gui_server, monkeypatch):
    limiter = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    all_fetches_started = threading.Event()
    release_fetches = threading.Event()
    state_lock = threading.Lock()
    active_fetches = 0
    peak_fetches = 0

    def blocking_fetch(**kwargs):
        nonlocal active_fetches, peak_fetches
        with state_lock:
            active_fetches += 1
            peak_fetches = max(peak_fetches, active_fetches)
            if active_fetches == gui.MAX_CONCURRENT_FETCHES:
                all_fetches_started.set()
        try:
            release_fetches.wait(timeout=2)
            return FetchResult(stations=[_station("PL1", "Warsaw")], source="remote", metadata={})
        finally:
            with state_lock:
                active_fetches -= 1

    monkeypatch.setattr(gui, "_FETCH_LIMITER", limiter)
    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", blocking_fetch)

    with ThreadPoolExecutor(max_workers=gui.MAX_CONCURRENT_FETCHES) as executor:
        requests = [
            executor.submit(_request, gui_server, "POST", "/api/search", {})
            for _ in range(gui.MAX_CONCURRENT_FETCHES)
        ]
        try:
            assert all_fetches_started.wait(timeout=1)

            busy_status, busy_headers, busy_body = _request(gui_server, "POST", "/api/search", {})
            busy_payload = json.loads(busy_body)
            health_status, _, health_body = _request(gui_server, "GET", "/health")

            assert busy_status == 503
            assert busy_payload["code"] == "SERVER_BUSY"
            assert busy_headers["x-request-id"] == busy_payload["request_id"]
            assert health_status == 200
            assert json.loads(health_body)["ok"] is True
            assert peak_fetches == gui.MAX_CONCURRENT_FETCHES
        finally:
            release_fetches.set()

        assert all(request.result()[0] == 200 for request in requests)

    acquired_slots = [limiter.acquire(blocking=False) for _ in range(gui.MAX_CONCURRENT_FETCHES)]
    assert all(acquired_slots)
    for _ in acquired_slots:
        limiter.release()


def test_app_http_server_uses_daemon_request_threads():
    assert gui.AppHTTPServer.daemon_threads is True
    assert gui.AppHTTPServer.allow_reuse_address is True


def test_server_shutdown_stops_serving_thread():
    server = gui.AppHTTPServer(("127.0.0.1", 0), gui.AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    server.shutdown()
    server.server_close()
    thread.join(timeout=1)

    assert thread.is_alive() is False


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


def test_search_confines_cache_file_to_gui_cache_directory(gui_server, monkeypatch, tmp_path):
    captured = {}

    def capture_fetch(**kwargs):
        captured.update(kwargs)
        return FetchResult(stations=[], source="sample-default", metadata={})

    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", capture_fetch)

    status, _, _ = _request(gui_server, "POST", "/api/search", {"cache_path": "stations.json"})

    assert status == 200
    assert captured["cache_path"] == tmp_path / "stations.json"
    assert tmp_path.is_dir()


@pytest.mark.parametrize(
    "cache_path",
    ["../outside.json", "nested/cache.json", "C:\\outside.json", "C:outside.json", "notes.txt"],
)
def test_search_rejects_unsafe_cache_path(gui_server, monkeypatch, cache_path):
    fetch_called = False

    def capture_fetch(**kwargs):
        nonlocal fetch_called
        fetch_called = True
        return FetchResult(stations=[], source="sample-default", metadata={})

    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", capture_fetch)

    status, _, body = _request(gui_server, "POST", "/api/search", {"cache_path": cache_path})

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"
    assert fetch_called is False


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
