from concurrent.futures import ThreadPoolExecutor

from openapi_spec_validator import validate

from dane_meteo_stacje import __version__
from dane_meteo_stacje.api_contract import OPENAPI_DOCUMENT
from dane_meteo_stacje.metrics import MetricsRegistry


def test_openapi_contract_is_versioned_and_covers_all_http_routes():
    validate(OPENAPI_DOCUMENT)
    assert OPENAPI_DOCUMENT["openapi"] == "3.1.0"
    assert OPENAPI_DOCUMENT["info"]["version"] == __version__
    assert set(OPENAPI_DOCUMENT["paths"]) == {
        "/",
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/openapi.json",
        "/api/search",
        "/api/temperature-capabilities",
        "/api/temperatures",
        "/api/export",
    }
    assert "504" in OPENAPI_DOCUMENT["paths"]["/api/search"]["post"]["responses"]
    error_schema = OPENAPI_DOCUMENT["components"]["schemas"]["Error"]
    assert set(error_schema["required"]) == {"code", "message", "request_id"}


def test_metrics_registry_is_thread_safe_and_exports_required_signals():
    registry = MetricsRegistry()

    def record_request(index: int) -> None:
        registry.record_request("GET", "/health/live", 200, 0.01)
        if index % 2 == 0:
            registry.record_fallback("cache-stale")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record_request, range(20)))

    registry.record_request("POST", "/api/search", 503, 0.02)
    registry.record_server_busy()
    registry.fetch_started()
    metrics = registry.render_prometheus()
    registry.fetch_finished()

    assert 'method="GET",path="/health/live",status="200"} 20' in metrics
    assert 'dane_meteo_errors_total{status="503"} 1' in metrics
    assert 'dane_meteo_fallbacks_total{source="cache-stale"} 10' in metrics
    assert "dane_meteo_server_busy_total 1" in metrics
    assert "dane_meteo_active_fetches 1" in metrics
    assert "dane_meteo_request_duration_seconds_count 21" in metrics
