import io
import json
import logging

import pytest

import dane_meteo_stacje.data as data
from dane_meteo_stacje.observability import (
    bind_request_id,
    configure_logging,
    log_event,
    reset_request_id,
)


def test_log_event_emits_structured_json():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    log_event(
        "station_fetch_completed",
        request_id="request-123",
        source="remote",
        count=2,
    )

    payload = json.loads(stream.getvalue())
    assert payload == {
        "count": 2,
        "event": "station_fetch_completed",
        "request_id": "request-123",
        "source": "remote",
    }


def test_log_level_filters_debug_events():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    log_event("debug_event", level=logging.DEBUG)

    assert stream.getvalue() == ""


def test_configure_logging_rejects_unknown_level():
    with pytest.raises(ValueError, match="Unsupported log level"):
        configure_logging("TRACE")


def test_request_id_context_is_added_to_nested_events():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    token = bind_request_id("request-context")
    try:
        log_event("nested_event")
    finally:
        reset_request_id(token)

    assert json.loads(stream.getvalue())["request_id"] == "request-context"


def test_station_fetch_logs_safe_metadata_without_token(monkeypatch):
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    station = {
        "station_id": "PL1",
        "city": "Warsaw",
        "name": "Warsaw Station",
        "country": "Poland",
    }

    monkeypatch.setattr(
        data,
        "fetch_remote_stations",
        lambda *args, **kwargs: ([station], {"http_status": 200, "payload_shape": "stations-list"}),
    )
    result = data.fetch_stations_with_cache_details(
        remote_url="https://www.ncei.noaa.gov/cdo-web/api/v2/stations",
        token="super-secret-token",
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert result.source == "remote"
    assert [event["event"] for event in events] == ["station_fetch_started", "station_fetch_completed"]
    assert events[-1]["remote_host"] == "www.ncei.noaa.gov"
    assert "super-secret-token" not in stream.getvalue()
