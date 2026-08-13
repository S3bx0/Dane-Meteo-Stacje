import hashlib

import pytest

from dane_meteo_stacje.data import (
    NoaaAuthError,
    NoaaRateLimitError,
    TokenProvider,
    fetch_remote_stations,
    private_env_file_path,
    resolve_env_file,
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


def test_token_provider_waits_for_per_token_request_slot():
    now = [100.0]
    sleeps = []

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    provider = TokenProvider(
        ["token"],
        request_interval_seconds=0.25,
        now_fn=lambda: now[0],
        sleep_fn=advance,
    )

    assert provider.has_available_token() is True
    assert provider.acquire() == "token"
    assert provider.acquire() is None
    assert provider.acquire(wait_for_slot=True) == "token"
    assert sleeps == [0.25]


def test_token_provider_noop_markers_accept_missing_token():
    provider = TokenProvider([])
    provider.mark_rate_limited(None)
    provider.mark_auth_failed(None)

    assert provider.acquire() is None


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
    assert metadata["token_fingerprint"] == hashlib.sha256(b"good").hexdigest()[:10]


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
    assert metadata["token_fingerprint"] == hashlib.sha256(b"fresh").hexdigest()[:10]


def test_fetch_remote_stations_tries_every_token_before_failing_over():
    provider = TokenProvider(["bad-1", "bad-2", "good"], now_fn=lambda: 0.0)
    fake_client = FakeNoaaClient({"bad-1": "auth", "bad-2": "rate-limit", "good": "ok"})

    stations, _ = fetch_remote_stations(
        "https://example.invalid/stations",
        client=fake_client,
        token_provider=provider,
    )

    assert stations[0]["station_id"] == "T1"
    assert fake_client.calls == ["bad-1", "bad-2", "good"]


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


def test_token_provider_loads_project_dotenv_without_overriding_process_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DANE_METEO_ENV_FILE", raising=False)
    monkeypatch.delenv("NOAA_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKEN", raising=False)
    monkeypatch.setenv("NOAA_API_TOKENS", "process-token")
    (tmp_path / ".env").write_text("NOAA_API_TOKENS=file-token\nNOAA_TOKENS=file-pool\n", encoding="utf-8")

    provider = TokenProvider.from_env()

    assert provider.acquire() == "process-token"
    assert provider.acquire() == "file-pool"


def test_token_provider_loads_explicit_dotenv_file(monkeypatch, tmp_path):
    env_file = tmp_path / "tokens.env"
    env_file.write_text("NOAA_API_TOKENS=file-a, file-b\n", encoding="utf-8")
    monkeypatch.setenv("DANE_METEO_ENV_FILE", str(env_file))
    monkeypatch.delenv("NOAA_API_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKEN", raising=False)

    provider = TokenProvider.from_env()

    assert provider.acquire() == "file-a"
    assert provider.acquire() == "file-b"


def test_token_provider_reloads_dotenv_when_file_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DANE_METEO_ENV_FILE", raising=False)
    monkeypatch.delenv("NOAA_API_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKENS", raising=False)
    monkeypatch.delenv("NOAA_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("NOAA_API_TOKENS=first-token\n", encoding="utf-8")

    first_provider = TokenProvider.from_env()
    env_file.write_text("NOAA_API_TOKENS=second-token\n", encoding="utf-8")
    second_provider = TokenProvider.from_env()

    assert first_provider.acquire() == "first-token"
    assert second_provider.acquire() == "second-token"


def test_private_env_file_has_priority_over_legacy_project_file(monkeypatch, tmp_path):
    local_app_data = tmp_path / "local"
    private_file = local_app_data / "Dane-Meteo-Stacje" / ".env"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("NOAA_API_TOKENS=private-token\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("NOAA_API_TOKENS=legacy-token\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("DANE_METEO_ENV_FILE", raising=False)
    for name in ("NOAA_API_TOKENS", "NOAA_TOKENS", "NOAA_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    assert private_env_file_path() == private_file
    assert resolve_env_file() == private_file
    assert TokenProvider.from_env().acquire() == "private-token"


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
