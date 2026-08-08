import hashlib

import pytest

from dane_meteo_stacje.data import (
    NoaaAuthError,
    NoaaRateLimitError,
    TokenProvider,
    fetch_remote_stations,
)


class FakeNoaaClient:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def fetch_json(self, url: str, token: str | None = None):
        self.calls.append(token)
        action = self.behavior.get(token, "ok")
        if action == "rate-limit":
            raise NoaaRateLimitError("HTTP 429")
        if action == "auth":
            raise NoaaAuthError("HTTP 401")

        return (
            [
                {
                    "station_id": "T1",
                    "city": "Token City",
                    "name": "Token Station",
                    "country": "Poland",
                }
            ],
            200,
            {"etag": "etag-1", "last_modified": "Fri, 01 Jan 2021 00:00:00 GMT"},
        )


def test_token_provider_round_robin_and_blocking_windows():
    now = [1000.0]

    provider = TokenProvider(
        ["token-a", "token-b"],
        rate_limit_cooldown_seconds=10,
        auth_quarantine_seconds=30,
        now_fn=lambda: now[0],
    )

    assert provider.acquire() == "token-a"
    assert provider.acquire() == "token-b"

    provider.mark_rate_limited("token-a")
    assert provider.acquire() == "token-b"

    provider.mark_auth_failed("token-b")
    assert provider.acquire() is None

    now[0] += 11
    assert provider.acquire() == "token-a"


def test_fetch_remote_stations_rotates_token_on_rate_limit():
    provider = TokenProvider(["bad", "good"], now_fn=lambda: 0.0)
    fake_client = FakeNoaaClient({"bad": "rate-limit", "good": "ok"})

    stations, metadata = fetch_remote_stations(
        "https://example.invalid/stations",
        client=fake_client,
        token_provider=provider,
    )

    assert [station["station_id"] for station in stations] == ["T1"]
    assert fake_client.calls == ["bad", "good"]
    assert metadata["token_fingerprint"] == hashlib.sha256("good".encode("utf-8")).hexdigest()[:10]


def test_fetch_remote_stations_rotates_token_on_auth_error():
    provider = TokenProvider(["expired", "fresh"], now_fn=lambda: 0.0)
    fake_client = FakeNoaaClient({"expired": "auth", "fresh": "ok"})

    stations, metadata = fetch_remote_stations(
        "https://example.invalid/stations",
        client=fake_client,
        token_provider=provider,
    )

    assert [station["station_id"] for station in stations] == ["T1"]
    assert fake_client.calls == ["expired", "fresh"]
    assert metadata["token_fingerprint"] == hashlib.sha256("fresh".encode("utf-8")).hexdigest()[:10]


def test_fetch_remote_stations_fails_when_all_tokens_unhealthy():
    now = [1000.0]
    provider = TokenProvider(["only-token"], auth_quarantine_seconds=60, now_fn=lambda: now[0])
    provider.mark_auth_failed("only-token")
    fake_client = FakeNoaaClient({"only-token": "ok"})

    with pytest.raises(NoaaAuthError):
        fetch_remote_stations(
            "https://example.invalid/stations",
            client=fake_client,
            token_provider=provider,
        )


def test_token_provider_from_env_supports_noaa_api_tokens(monkeypatch):
    monkeypatch.setenv("NOAA_API_TOKENS", "api-a, api-b")
    monkeypatch.setenv("NOAA_TOKENS", "pool-a")
    monkeypatch.setenv("NOAA_TOKEN", "single-a")

    provider = TokenProvider.from_env()

    # Preserves priority order NOAA_API_TOKENS -> NOAA_TOKENS -> NOAA_TOKEN.
    assert provider.acquire() == "api-a"
    assert provider.acquire() == "api-b"
    assert provider.acquire() == "pool-a"
    assert provider.acquire() == "single-a"


def test_fetch_remote_stations_reports_noaa_normalization_quality():
    class DictPayloadClient:
        def fetch_json(self, url: str, token: str | None = None):
            return (
                {
                    "results": [
                        {"id": "USW00011111", "name": "BALTIMORE STATION", "country": "USA"},
                        {"id": "", "name": "Missing ID"},
                        "not-an-object",
                    ]
                },
                200,
                {"etag": "etag-x", "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
            )

    stations, metadata = fetch_remote_stations(
        "https://example.invalid/stations",
        client=DictPayloadClient(),
        token="token-x",
    )

    assert len(stations) == 1
    assert metadata["payload_shape"] == "noaa-results"
    assert metadata["normalization"]["items_total"] == 3
    assert metadata["normalization"]["items_valid"] == 1
    assert metadata["normalization"]["items_invalid"] == 2
