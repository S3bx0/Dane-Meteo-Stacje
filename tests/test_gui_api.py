import http.client
import io
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import dane_meteo_stacje.gui_bootstrap as gui
from dane_meteo_stacje.data import FetchResult, NoaaNetworkError, NoaaTimeoutError
from dane_meteo_stacje.metrics import MetricsRegistry
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


def test_get_routes(gui_server, monkeypatch):
    monkeypatch.delenv("NOAA_API_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKEN", raising=False)
    monkeypatch.setattr(gui.TokenProvider, "configured_tokens", classmethod(lambda cls: []))
    status, headers, body = _request(gui_server, "GET", "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert b"Dane Meteo Stacje" in body
    assert b"Station ID filter (optional)" in body
    assert b"leave empty for every station" in body
    assert b"private per-user .env file" in body
    assert b'id="station-map"' in body
    assert b'id="map-fit-btn"' in body
    assert b'id="map-reset-btn"' in body
    assert b'id="map-country-boundary"' in body
    assert b'id="quality-min-years"' in body
    assert b'id="quality-min-coverage"' in body
    assert b'id="best-station-btn"' in body
    assert b'id="temperature-preview-chart"' in body
    assert b'id="amplitude-preview-chart"' in body
    assert b'id="nearby-quality-body"' in body
    assert b'id="station-comparison-panel"' in body
    assert b'id="comparison-selection"' in body
    assert b'id="comparison-temperature-chart"' in body
    assert b'id="comparison-summary-body"' in body
    assert b'id="comparison-distance-body"' in body
    assert b"/static/vendor/leaflet/leaflet.js" in body
    assert b"/static/vendor/leaflet-markercluster/leaflet.markercluster.js" in body
    assert b"function initializeStationMap" in body
    assert b"function ensureMapTiles" in body
    assert b"function stationClusterIcon" in body
    assert b"function fitMapToStations" in body
    assert b"function focusMapOnStation" in body
    assert b"function loadCountryBoundary" in body
    assert b'fillOpacity: 0.12' in body
    assert b'fetch("/api/country-boundary"' in body
    assert b"function selectBestStation" in body
    assert b"function loadTemperaturePreview" in body
    assert b"function renderNearbyQualityComparison" in body
    assert b"function toggleComparisonStation" in body
    assert b"function loadStationComparison" in body
    assert b"function renderStationComparison" in body
    assert b"function renderDistanceMatrix" in body
    assert b"https://tile.openstreetmap.org/{z}/{x}/{y}.png" in body
    assert b"html: '<span aria-hidden=\"true\"></span>'" in body
    assert b"target=\"_blank\" rel=\"noopener noreferrer\"" in body
    assert "unsafe-inline" not in headers["content-security-policy"].split("script-src", 1)[1].split(";", 1)[0]
    csp_nonce = headers["content-security-policy"].split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{csp_nonce}">'.encode() in body
    csp_directives: dict[str, set[str]] = {}
    for directive in headers["content-security-policy"].split(";"):
        tokens = directive.split()
        if tokens:
            csp_directives[tokens[0]] = set(tokens[1:])
    assert "https://tile.openstreetmap.org" in csp_directives["img-src"]


@pytest.mark.parametrize("use_private", [True, False])
def test_home_reports_configured_token_source(gui_server, monkeypatch, tmp_path, use_private):
    private_file = tmp_path / "private.env"
    selected_file = private_file if use_private else tmp_path / "legacy.env"
    monkeypatch.setattr(gui.TokenProvider, "configured_tokens", classmethod(lambda cls: ["token"]))
    monkeypatch.setattr(gui, "private_env_file_path", lambda: private_file)
    monkeypatch.setattr(gui, "resolve_env_file", lambda: selected_file)

    status, _, body = _request(gui_server, "GET", "/")

    assert status == 200
    expected = b"configured in the private per-user .env file" if use_private else b"legacy project .env"
    assert expected in body


def test_missing_static_asset_returns_safe_404(gui_server, monkeypatch, tmp_path):
    assets = dict(gui._STATIC_ASSETS)
    assets["/static/missing.js"] = (tmp_path / "missing.js", "text/javascript; charset=utf-8")
    monkeypatch.setattr(gui, "_STATIC_ASSETS", assets)

    status, _, body = _request(gui_server, "GET", "/static/missing.js")

    assert status == 404
    assert json.loads(body)["code"] == "STATIC_ASSET_UNAVAILABLE"

    status, static_headers, static_body = _request(gui_server, "GET", "/static/vendor/leaflet/leaflet.js")
    assert status == 200
    assert static_headers["content-type"] == "text/javascript; charset=utf-8"
    assert static_headers["x-content-type-options"] == "nosniff"
    assert static_headers["cache-control"] == "public, max-age=31536000, immutable"
    assert b"Leaflet 1.9.4" in static_body

    status, static_headers, static_body = _request(
        gui_server,
        "GET",
        "/static/vendor/leaflet-markercluster/MarkerCluster.css",
    )
    assert status == 200
    assert static_headers["content-type"] == "text/css; charset=utf-8"
    assert b"leaflet-cluster-anim" in static_body

    status, headers, body = _request(gui_server, "GET", "/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["status"] == "alive"
    assert len(payload["request_id"]) == 32
    assert headers["x-request-id"] == payload["request_id"]

    status, _, body = _request(gui_server, "GET", "/missing")
    assert status == 404
    assert json.loads(body)["code"] == "NOT_FOUND"


def test_operability_routes_expose_contract_health_and_metrics(gui_server, monkeypatch):
    registry = MetricsRegistry()
    monkeypatch.setattr(gui, "_METRICS", registry)
    monkeypatch.setenv("NOAA_API_TOKENS", "test-token")

    live_status, live_headers, live_body = _request(gui_server, "GET", "/health/live")
    ready_status, _, ready_body = _request(gui_server, "GET", "/health/ready")
    contract_status, _, contract_body = _request(gui_server, "GET", "/openapi.json")
    metrics_status, metrics_headers, metrics_body = _request(gui_server, "GET", "/metrics")

    assert live_status == ready_status == contract_status == metrics_status == 200
    assert json.loads(live_body)["status"] == "alive"
    ready = json.loads(ready_body)
    assert ready["status"] == "ready"
    assert ready["checks"]["noaa_token"] == "configured"
    contract = json.loads(contract_body)
    assert contract["openapi"] == "3.1.0"
    assert "request_id" not in contract
    assert metrics_headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert b'dane_meteo_requests_total{method="GET",path="/health/live",status="200"} 1' in metrics_body
    assert live_headers["x-content-type-options"] == "nosniff"
    assert live_headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert live_headers["cache-control"] == "no-store"


def test_readiness_reports_missing_noaa_token(gui_server, monkeypatch, tmp_path):
    for name in ("NOAA_API_TOKENS", "NOAA_TOKENS", "NOAA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DANE_METEO_ENV_FILE", str(tmp_path / "missing.env"))

    status, _, body = _request(gui_server, "GET", "/health/ready")
    payload = json.loads(body)

    assert status == 503
    assert payload["ok"] is False
    assert payload["status"] == "not_ready"
    assert payload["checks"]["noaa_token"] == "missing"


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
        "fetch_stations_for_country",
        lambda *args, **kwargs: FetchResult(stations=stations, source="remote", metadata={"http_status": 200}),
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
    assert payload["results"][0]["quality"]["assessment"] == "catalogue"
    assert payload["results"][0]["quality"]["grade"] == "weak"


def test_query_search_applies_country_and_station_filters(gui_server, monkeypatch):
    stations = [_station("PL1", "Warsaw"), _station("PL2", "Gdansk")]
    monkeypatch.setattr(
        gui,
        "fetch_stations_for_country",
        lambda *args, **kwargs: FetchResult(stations=stations, source="cache-fresh", metadata={}),
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


def test_search_maps_noaa_deadline_to_504_and_passes_budget(gui_server, monkeypatch):
    captured = {}

    def timeout_fetch(**kwargs):
        captured.update(kwargs)
        raise NoaaTimeoutError("deadline")

    monkeypatch.setattr(gui, "fetch_stations_with_cache_details", timeout_fetch)
    status, headers, body = _request(gui_server, "POST", "/api/search", {})
    payload = _assert_json_contract(headers, body, error_code="NOAA_TIMEOUT")

    assert status == 504
    assert captured["remote_timeout_seconds"] == gui.REMOTE_REQUEST_DEADLINE_SECONDS
    assert "deadline" not in payload["message"]


def test_search_filters_station_id_sorts_name_and_records_fallback(gui_server, monkeypatch):
    rows = [
        {"station_id": "PL000000002", "city": "Beta", "name": "Zulu", "country": "Poland"},
        {"station_id": "PL000000001", "city": "Alpha", "name": "Alpha", "country": "Poland"},
    ]
    monkeypatch.setattr(
        gui,
        "fetch_stations_with_cache_details",
        lambda **kwargs: FetchResult(stations=rows, source="cache-stale", metadata={}),
    )

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/search",
        {"station_id": "PL000000001", "sort_by": "name"},
    )

    assert status == 200
    assert [row["station_id"] for row in json.loads(body)["results"]] == ["PL000000001"]


def test_country_boundary_endpoint_fetches_geojson_and_reuses_cache(
    gui_server,
    monkeypatch,
    tmp_path,
):
    geometry = {
        "type": "Polygon",
        "coordinates": [[[14.1, 54.8], [24.1, 54.8], [24.1, 49.0], [14.1, 54.8]]],
    }
    calls = []

    class FakeResponse:
        headers = {"Content-Length": "256"}
        content = b"{}"

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list[dict[str, Any]]:
            return [{"display_name": "Polska", "geojson": geometry}]

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(gui, "_LAST_BOUNDARY_FETCH_AT", 0.0)
    monkeypatch.setattr(gui.requests, "get", fake_get)

    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/country-boundary",
        {"country": "Poland"},
    )
    response = _assert_json_contract(headers, body)
    assert status == 200
    assert response["source"] == "nominatim"
    assert response["data"]["geometry"] == geometry
    assert calls[0][0] == "https://nominatim.openstreetmap.org/search"
    assert calls[0][1]["params"]["featureType"] == "country"
    assert calls[0][1]["params"]["polygon_geojson"] == "1"
    assert "Dane-Meteo-Stacje" in calls[0][1]["headers"]["User-Agent"]

    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/country-boundary",
        {"country": "Poland"},
    )
    response = _assert_json_contract(headers, body)
    assert status == 200
    assert response["source"] == "cache"
    assert len(calls) == 1


def test_country_boundary_endpoint_validates_country(gui_server):
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/country-boundary",
        {"country": "Atlantis"},
    )
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_country_boundary_cache_rejects_stale_and_invalid_payloads(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    cache_path = gui._country_boundary_cache_path("Poland")
    cache_path.parent.mkdir(parents=True)
    feature = {
        "type": "Feature",
        "properties": {"country": "Poland"},
        "geometry": {"type": "Polygon", "coordinates": []},
    }
    cache_path.write_text(json.dumps(feature), encoding="utf-8")
    stale_now = cache_path.stat().st_mtime + gui.COUNTRY_BOUNDARY_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(gui.time, "time", lambda: stale_now)

    assert gui._read_country_boundary_cache("Poland") is None
    assert gui._read_country_boundary_cache("Poland", allow_stale=True) == feature

    cache_path.write_text(json.dumps([]), encoding="utf-8")
    assert gui._read_country_boundary_cache("Poland", allow_stale=True) is None
    cache_path.write_text(
        json.dumps({**feature, "geometry": {"type": "Point", "coordinates": []}}),
        encoding="utf-8",
    )
    assert gui._read_country_boundary_cache("Poland", allow_stale=True) is None
    cache_path.write_text("{", encoding="utf-8")
    assert gui._read_country_boundary_cache("Poland", allow_stale=True) is None


def test_country_boundary_rechecks_cache_inside_lock(monkeypatch):
    feature = {
        "type": "Feature",
        "properties": {"country": "Poland"},
        "geometry": {"type": "Polygon", "coordinates": []},
    }
    cached_values = iter([None, feature])
    monkeypatch.setattr(gui, "_read_country_boundary_cache", lambda *args, **kwargs: next(cached_values))

    assert gui.fetch_country_boundary("Poland") == (feature, "cache")


@pytest.mark.parametrize(
    ("headers", "content", "payload", "message"),
    [
        (
            {"Content-Length": str(gui.COUNTRY_BOUNDARY_MAX_BYTES + 1)},
            b"{}",
            [{"geojson": {"type": "Polygon", "coordinates": []}}],
            "too large",
        ),
        ({"Content-Length": "2"}, b"[]", [], "not found"),
        (
            {"Content-Length": "64"},
            b"{}",
            [{"geojson": {"type": "Point", "coordinates": []}}],
            "geometry",
        ),
    ],
)
def test_country_boundary_rejects_invalid_remote_payloads(
    monkeypatch,
    tmp_path,
    headers,
    content,
    payload,
    message,
):
    class FakeResponse:
        def __init__(self):
            self.headers = headers
            self.content = content

        @staticmethod
        def raise_for_status() -> None:
            return None

        def json(self):
            return payload

    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(gui, "_LAST_BOUNDARY_FETCH_AT", 0.0)
    monkeypatch.setattr(gui.requests, "get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(gui.CountryBoundaryError, match=message):
        gui.fetch_country_boundary("Poland")


def test_country_boundary_uses_stale_cache_on_network_error(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(gui, "_LAST_BOUNDARY_FETCH_AT", 0.0)
    feature = {
        "type": "Feature",
        "properties": {"country": "Poland"},
        "geometry": {"type": "Polygon", "coordinates": []},
    }
    gui._write_country_boundary_cache("Poland", feature)
    cache_path = gui._country_boundary_cache_path("Poland")
    stale_now = cache_path.stat().st_mtime + gui.COUNTRY_BOUNDARY_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(gui.time, "time", lambda: stale_now)
    monkeypatch.setattr(
        gui.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(gui.requests.RequestException("offline")),
    )

    assert gui.fetch_country_boundary("Poland") == (feature, "cache-stale")


def test_country_boundary_maps_network_error_without_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "GUI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(gui, "_LAST_BOUNDARY_FETCH_AT", 0.0)
    monkeypatch.setattr(
        gui.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(gui.requests.RequestException("offline")),
    )

    with pytest.raises(gui.CountryBoundaryError, match="service is unavailable"):
        gui.fetch_country_boundary("Poland")


def test_country_boundary_endpoint_requires_country_and_maps_service_error(gui_server, monkeypatch):
    status, _, body = _request(gui_server, "POST", "/api/country-boundary", {})
    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"

    monkeypatch.setattr(
        gui,
        "fetch_country_boundary",
        lambda country: (_ for _ in ()).throw(gui.CountryBoundaryError("offline")),
    )
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/country-boundary",
        {"country": "Poland"},
    )
    assert status == 502
    assert json.loads(body)["code"] == "COUNTRY_BOUNDARY_UNAVAILABLE"


def test_temperature_endpoint_returns_heatmap_payload(gui_server, monkeypatch):
    heatmap_payload = {
        "station_id": "PLM00012295",
        "years": [2023],
        "months": [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        "temperatures": [[1.25] + [None] * 11],
        "final_missing_years": [],
        "missing_data_report": {},
        "token_usage": {"toke...en-a": 1},
        "adaptive_history": [],
    }
    captured = {}

    def fake_fetch(station_id, start_year, end_year, **kwargs):
        captured.update(station_id=station_id, start_year=start_year, end_year=end_year)
        captured.update(kwargs)
        return heatmap_payload

    monkeypatch.setattr(gui, "fetch_monthly_temperature_matrix", fake_fetch)
    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {"station_id": "GHCND:PLM00012295", "start_year": 2023, "end_year": 2023},
    )
    response = _assert_json_contract(headers, body)

    assert status == 200
    assert response["data"] == heatmap_payload
    assert captured == {
        "station_id": "GHCND:PLM00012295",
        "start_year": 2023,
        "end_year": 2023,
        "cache_dir": gui.GUI_CACHE_DIR / "temperatures",
    }


def test_temperature_capabilities_endpoint_returns_station_datatypes(gui_server, monkeypatch):
    capabilities = {
        "station_id": "PLM00012295",
        "dataset_id": "GHCND",
        "available_datatypes": ["TAVG", "TMAX", "TMIN"],
        "core_temperature_datatypes": ["TMIN", "TAVG", "TMAX"],
        "datatype_details": [],
        "derived_datatypes": {"TAXN": True, "AMPLITUDE": True},
        "export_modes": {"heatmap": True, "daily": True, "monthly": True, "extended": True},
        "temperature_methods": {},
    }
    monkeypatch.setattr(gui, "fetch_station_temperature_capabilities", lambda station_id: capabilities)

    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/temperature-capabilities",
        {"station_id": "GHCND:PLM00012295"},
    )
    response = _assert_json_contract(headers, body)

    assert status == 200
    assert response["data"] == capabilities


def test_temperature_capabilities_endpoint_requires_station_id(gui_server):
    status, _, body = _request(gui_server, "POST", "/api/temperature-capabilities", {})

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_temperature_capabilities_endpoint_reports_busy_server(gui_server, monkeypatch):
    exhausted = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    for _ in range(gui.MAX_CONCURRENT_FETCHES):
        assert exhausted.acquire(blocking=False)
    monkeypatch.setattr(gui, "_FETCH_LIMITER", exhausted)

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperature-capabilities",
        {"station_id": "GHCND:PLM00012295"},
    )

    assert status == 503
    assert json.loads(body)["code"] == "SERVER_BUSY"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (ValueError("bad station"), 400, "BAD_REQUEST"),
        (NoaaTimeoutError("deadline"), 504, "NOAA_TIMEOUT"),
        (NoaaNetworkError("offline"), 502, "NOAA_NETWORK"),
    ],
)
def test_temperature_capabilities_endpoint_maps_failures(
    gui_server,
    monkeypatch,
    error,
    expected_status,
    expected_code,
):
    monkeypatch.setattr(
        gui,
        "fetch_station_temperature_capabilities",
        lambda station_id: (_ for _ in ()).throw(error),
    )

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperature-capabilities",
        {"station_id": "GHCND:PLM00012295"},
    )

    assert status == expected_status
    assert json.loads(body)["code"] == expected_code


def test_temperature_endpoint_dispatches_daily_export_mode(gui_server, monkeypatch):
    exported = {
        "schema_version": 1,
        "export_type": "daily",
        "station_id": "PLM00012295",
        "period": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "data": [],
    }
    captured = {}

    def fake_fetch(station_id, start_year, end_year, **kwargs):
        captured.update(station_id=station_id, start_year=start_year, end_year=end_year)
        captured.update(kwargs)
        return exported

    monkeypatch.setattr(gui, "fetch_temperature_export", fake_fetch)
    status, headers, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {
            "station_id": "GHCND:PLM00012295",
            "start_year": 2024,
            "end_year": 2024,
            "mode": "daily",
        },
    )
    response = _assert_json_contract(headers, body)

    assert status == 200
    assert response["data"] == exported
    assert captured == {
        "station_id": "GHCND:PLM00012295",
        "start_year": 2024,
        "end_year": 2024,
        "mode": "daily",
        "cache_dir": gui.GUI_CACHE_DIR / "temperatures",
    }


def test_temperature_endpoint_validates_years(gui_server):
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {"station_id": "PLM00012295", "start_year": 2025, "end_year": 2020},
    )

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_temperature_endpoint_rejects_unknown_mode(gui_server):
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {"station_id": "PLM00012295", "start_year": 2024, "end_year": 2024, "mode": "hourly"},
    )

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    "payload",
    [
        {"start_year": 2023, "end_year": 2023},
        {"station_id": "PLM00012295"},
    ],
)
def test_temperature_endpoint_requires_station_and_years(gui_server, payload):
    status, _, body = _request(gui_server, "POST", "/api/temperatures", payload)

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_temperature_endpoint_reports_busy_server(gui_server, monkeypatch):
    exhausted = threading.BoundedSemaphore(gui.MAX_CONCURRENT_FETCHES)
    for _ in range(gui.MAX_CONCURRENT_FETCHES):
        assert exhausted.acquire(blocking=False)
    monkeypatch.setattr(gui, "_FETCH_LIMITER", exhausted)

    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {"station_id": "PLM00012295", "start_year": 2023, "end_year": 2023},
    )

    assert status == 503
    assert json.loads(body)["code"] == "SERVER_BUSY"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (ValueError("bad range"), 400, "BAD_REQUEST"),
        (NoaaTimeoutError("deadline"), 504, "NOAA_TIMEOUT"),
        (NoaaNetworkError("offline"), 502, "NOAA_NETWORK"),
    ],
)
def test_temperature_endpoint_maps_fetch_failures(
    gui_server,
    monkeypatch,
    error,
    expected_status,
    expected_code,
):
    def fail_fetch(*args, **kwargs):
        raise error

    monkeypatch.setattr(gui, "fetch_monthly_temperature_matrix", fail_fetch)
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/temperatures",
        {"station_id": "PLM00012295", "start_year": 2023, "end_year": 2023},
    )

    assert status == expected_status
    assert json.loads(body)["code"] == expected_code


def test_search_rejects_country_missing_from_noaa_catalogue(gui_server):
    status, _, body = _request(
        gui_server,
        "POST",
        "/api/search",
        {"country": "Atlantis"},
    )

    assert status == 400
    assert json.loads(body)["code"] == "BAD_REQUEST"


def test_run_server_opens_browser_and_closes_after_interrupt(monkeypatch):
    events = []

    class FakeServer:
        def __init__(self, address, handler):
            events.append(("init", address, handler))

        def serve_forever(self):
            events.append(("serve",))
            raise KeyboardInterrupt

        def server_close(self):
            events.append(("close",))

    monkeypatch.setattr(gui, "AppHTTPServer", FakeServer)
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: events.append(("open", url)))

    gui.run_server("127.0.0.1", 8765, open_browser=True)

    assert events[0][0:2] == ("init", ("127.0.0.1", 8765))
    assert ("open", "http://127.0.0.1:8765") in events
    assert events[-2:] == [("serve",), ("close",)]


def test_main_requires_explicit_network_binding_and_allows_opt_in(monkeypatch):
    with pytest.raises(SystemExit):
        gui.main(["--host", "not-an-ip", "--no-browser"])
    with pytest.raises(SystemExit):
        gui.main(["--host", "0.0.0.0", "--no-browser"])

    calls = []
    monkeypatch.setattr(gui, "run_server", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert gui.main(["--host", "0.0.0.0", "--allow-network", "--no-browser"]) == 0
    assert calls == [(('0.0.0.0', 8765), {"open_browser": False})]


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
