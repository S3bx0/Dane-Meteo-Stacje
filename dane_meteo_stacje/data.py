from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, Sequence, TypedDict

import requests


class NoaaClientError(Exception):
    """Base class for NOAA fetch errors."""


class NoaaAuthError(NoaaClientError):
    """Authentication or authorization failed."""


class NoaaRateLimitError(NoaaClientError):
    """Remote API rate limit hit."""


class NoaaNetworkError(NoaaClientError):
    """Remote API is unavailable or request failed."""


class NoaaPayloadError(NoaaClientError):
    """Remote API returned an invalid payload."""


@dataclass(frozen=True)
class FetchResult:
    stations: list[StationRecord]
    source: str
    metadata: dict[str, Any]


class StationRecord(TypedDict):
    station_id: str
    city: str
    name: str
    country: str
    source: NotRequired[str]
    notes: NotRequired[str]


class TokenProvider:
    def __init__(
        self,
        tokens: Sequence[str],
        *,
        rate_limit_cooldown_seconds: int = 30,
        auth_quarantine_seconds: int = 300,
        now_fn: Any | None = None,
    ) -> None:
        cleaned = [token.strip() for token in tokens if token and token.strip()]
        self._tokens = cleaned
        self._cursor = 0
        self._rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self._auth_quarantine_seconds = auth_quarantine_seconds
        self._now_fn = now_fn or time.time
        self._blocked_until: dict[str, float] = {}

    @classmethod
    def from_env(cls) -> "TokenProvider":
        tokens: list[str] = []
        tokens_env = os.getenv("NOAA_TOKENS", "")
        if tokens_env.strip():
            tokens.extend([token.strip() for token in tokens_env.split(",") if token.strip()])

        single_token = os.getenv("NOAA_TOKEN", "").strip()
        if single_token and single_token not in tokens:
            tokens.append(single_token)
        return cls(tokens)

    def has_tokens(self) -> bool:
        return bool(self._tokens)

    def has_available_token(self) -> bool:
        now = float(self._now_fn())
        return any(self._blocked_until.get(token, 0.0) <= now for token in self._tokens)

    def acquire(self) -> str | None:
        if not self._tokens:
            return None

        now = float(self._now_fn())
        total = len(self._tokens)
        for step in range(total):
            idx = (self._cursor + step) % total
            token = self._tokens[idx]
            if self._blocked_until.get(token, 0.0) <= now:
                self._cursor = (idx + 1) % total
                return token
        return None

    def mark_rate_limited(self, token: str | None) -> None:
        if not token:
            return
        self._blocked_until[token] = float(self._now_fn()) + float(self._rate_limit_cooldown_seconds)

    def mark_auth_failed(self, token: str | None) -> None:
        if not token:
            return
        self._blocked_until[token] = float(self._now_fn()) + float(self._auth_quarantine_seconds)


def _token_fingerprint(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]

STATIONS: list[StationRecord] = [
    {
        "station_id": "PLM00012295",
        "city": "Bialystok",
        "name": "Bialystok",
        "country": "Poland",
        "source": "NOAA example station",
        "notes": "Przykładowa stacja dla testów i demo",
    },
    {
        "station_id": "EZM00011520",
        "city": "Prague",
        "name": "Prague",
        "country": "Czechia",
        "source": "NOAA example station",
        "notes": "Przykładowa stacja dla testów i demo",
    },
]


class NoaaClient:
    def __init__(
        self,
        timeout: int = 10,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        user_agent: str = "dane-meteo-stacje/0.2",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.user_agent = user_agent
        self._session = requests.Session()

    def fetch_json(self, url: str, token: str | None = None) -> tuple[Any, int, dict[str, str]]:
        headers: dict[str, str] = {"User-Agent": self.user_agent}
        if token:
            # NOAA CDO uses the `token` header; Authorization is included for compatibility.
            headers["token"] = token
            headers["Authorization"] = f"Bearer {token}"

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.get(url, timeout=self.timeout, headers=headers)
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                raise NoaaNetworkError(str(exc)) from exc

            status = response.status_code
            if status in (401, 403):
                raise NoaaAuthError(f"HTTP {status}")
            if status == 429:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                raise NoaaRateLimitError("HTTP 429")
            if status >= 500:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                raise NoaaNetworkError(f"HTTP {status}")
            if status >= 400:
                raise NoaaNetworkError(f"HTTP {status}")

            try:
                metadata_headers = {
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                }
                return response.json(), status, metadata_headers
            except ValueError as exc:
                raise NoaaPayloadError("Response is not valid JSON") from exc

        raise NoaaNetworkError("Unexpected NOAA fetch failure")


def _normalize_station(item: Any) -> StationRecord | None:
    if not isinstance(item, dict):
        return None

    required_fields = {"station_id", "city", "name", "country"}
    if not required_fields.issubset(item.keys()):
        return None

    normalized: StationRecord = {
        "station_id": str(item["station_id"]),
        "city": str(item["city"]),
        "name": str(item["name"]),
        "country": str(item["country"]),
    }

    for key in ("source", "notes"):
        if key in item:
            normalized[key] = str(item[key])

    return normalized


def _extract_city_from_noaa_item(item: dict[str, Any], name: str) -> str:
    city_name = ""
    if isinstance(item.get("city"), str) and item.get("city", "").strip():
        city_name = str(item["city"]).split(",")[0].strip()

    if not city_name:
        location = item.get("location")
        if isinstance(location, dict):
            for key in ("city", "name", "region", "state", "station"):
                location_value = location.get(key)
                if isinstance(location_value, str) and location_value.strip():
                    city_name = location_value.split(",")[0].strip()
                    break

    if not city_name:
        for key in ("region", "state", "city", "location_name", "administrative_area"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                city_name = value.split(",")[0].strip()
                break

    if not city_name:
        name_text = str(name)
        if "WASHINGTON" in name_text.upper() and "BALTIMORE" in name_text.upper():
            city_name = "Baltimore"
        elif "NEWARK" in name_text.upper():
            city_name = "Newark"
        else:
            city_name = name_text.split(",")[0].strip()

    if not city_name:
        coords = item.get("coordinates")
        if isinstance(coords, dict):
            lat = coords.get("latitude")
            lon = coords.get("longitude")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                city_name = f"Lat {lat}, Lon {lon}"

    if not city_name:
        city_name = "Unknown"
    return city_name


def _infer_country_from_noaa_item(item: dict[str, Any], station_id: str) -> str:
    country = item.get("country")
    if isinstance(country, str) and country.strip():
        return country.strip()

    location = item.get("location")
    if isinstance(location, dict):
        location_country = location.get("country")
        if isinstance(location_country, str) and location_country.strip():
            return location_country.strip()

    if station_id.startswith("US"):
        return "USA"
    return "Unknown"


def _normalize_noaa_payload(payload: Any) -> list[StationRecord]:
    if isinstance(payload, dict):
        results = payload.get("results")
        if not isinstance(results, list):
            return []
    elif isinstance(payload, list):
        results = payload
    else:
        return []

    normalized_records: list[StationRecord] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        station_id = item.get("id") or item.get("station_id")
        name = item.get("name") or item.get("station") or item.get("id")
        if not station_id or not name:
            continue

        city_name = _extract_city_from_noaa_item(item, str(name))
        country_name = _infer_country_from_noaa_item(item, str(station_id))
        normalized_records.append(
            {
                "station_id": str(station_id),
                "city": city_name,
                "name": str(name),
                "country": country_name,
                "source": "NOAA",
                "notes": "Mapped from NOAA station payload",
            }
        )
    return normalized_records


def load_stations(source: str | Path | None = None) -> list[StationRecord]:
    if source is None:
        return [dict(station) for station in STATIONS]

    path = Path(source)
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and "stations" in payload and isinstance(payload["stations"], list):
        records = payload["stations"]
    else:
        return []

    normalized_records: list[StationRecord] = []
    for item in records:
        normalized = _normalize_station(item)
        if normalized is not None:
            normalized_records.append(normalized)

    return normalized_records


def fetch_remote_stations(
    url: str,
    timeout: int = 10,
    token: str | None = None,
    client: NoaaClient | None = None,
    token_provider: TokenProvider | None = None,
) -> tuple[list[StationRecord], dict[str, Any]]:
    noaa_client = client or NoaaClient(timeout=timeout)
    provider = token_provider
    if provider is None:
        if token:
            provider = TokenProvider([token])
        else:
            env_provider = TokenProvider.from_env()
            provider = env_provider if env_provider.has_tokens() else None

    active_token: str | None = None
    if provider is not None:
        active_token = provider.acquire()
        if active_token is None and provider.has_tokens():
            raise NoaaAuthError("No healthy NOAA token available")
    elif token:
        active_token = token

    try:
        payload, http_status, response_meta = noaa_client.fetch_json(url, token=active_token)
    except NoaaRateLimitError:
        if provider is not None and active_token:
            provider.mark_rate_limited(active_token)
            retry_token = provider.acquire()
            if retry_token is not None:
                payload, http_status, response_meta = noaa_client.fetch_json(url, token=retry_token)
                active_token = retry_token
            else:
                raise
        else:
            raise
    except NoaaAuthError:
        if provider is not None and active_token:
            provider.mark_auth_failed(active_token)
            retry_token = provider.acquire()
            if retry_token is not None:
                payload, http_status, response_meta = noaa_client.fetch_json(url, token=retry_token)
                active_token = retry_token
            else:
                raise
        else:
            raise

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("stations"), list):
        records = payload["stations"]
    else:
        records = _normalize_noaa_payload(payload)
        if records:
            return records, {
                "http_status": http_status,
                "payload_shape": "noaa-results",
                "token_fingerprint": _token_fingerprint(active_token),
                "etag": response_meta.get("etag") or None,
                "last_modified": response_meta.get("last_modified") or None,
            }
        raise NoaaPayloadError("Unsupported NOAA payload format")

    normalized_records: list[StationRecord] = []
    for item in records:
        normalized = _normalize_station(item)
        if normalized is not None:
            normalized_records.append(normalized)

    return normalized_records, {
        "http_status": http_status,
        "payload_shape": "stations-list",
        "token_fingerprint": _token_fingerprint(active_token),
        "etag": response_meta.get("etag") or None,
        "last_modified": response_meta.get("last_modified") or None,
    }


def _cache_metadata_path(cache_path: str | Path) -> Path:
    cache_file = Path(cache_path)
    return cache_file.with_suffix(cache_file.suffix + ".meta.json")


def read_cache_metadata(cache_path: str | Path) -> dict[str, Any]:
    metadata_path = _cache_metadata_path(cache_path)
    if not metadata_path.exists():
        return {}

    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache_metadata(
    cache_path: str | Path,
    payload: list[StationRecord],
    *,
    fetched_at: int | None = None,
    source_url: str | None = None,
    http_status: int | None = None,
    payload_shape: str | None = None,
    token_fingerprint: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    metadata_path = _cache_metadata_path(cache_path)
    fetch_ts = fetched_at if fetched_at is not None else int(time.time())
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": fetch_ts,
        "fetched_at": fetch_ts,
        "count": len(payload),
    }
    if source_url is not None:
        metadata["source_url"] = source_url
    if http_status is not None:
        metadata["http_status"] = http_status
    if payload_shape is not None:
        metadata["payload_shape"] = payload_shape
    if token_fingerprint is not None:
        metadata["token_fingerprint"] = token_fingerprint
    if etag is not None:
        metadata["etag"] = etag
    if last_modified is not None:
        metadata["last_modified"] = last_modified

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resolve_cache_age_seconds(cache_file: Path, cache_metadata: dict[str, Any]) -> int:
    fetched_at = cache_metadata.get("fetched_at") or cache_metadata.get("timestamp")
    now = int(time.time())
    if isinstance(fetched_at, int):
        return max(now - fetched_at, 0)
    if isinstance(fetched_at, float):
        return max(int(now - fetched_at), 0)
    return max(int(time.time() - cache_file.stat().st_mtime), 0)


def _cache_matches_remote_source(cache_metadata: dict[str, Any], remote_url: str | None) -> bool:
    if remote_url is None:
        return True
    cached_source = cache_metadata.get("source_url")
    if not isinstance(cached_source, str) or not cached_source.strip():
        # Legacy cache metadata without source_url are treated as compatible.
        return True
    return cached_source.strip() == remote_url.strip()


def fetch_stations_with_cache_details(
    cache_path: str | Path | None = None,
    remote_url: str | None = None,
    cache_ttl: int = 3600,
    refresh: bool = False,
    allow_sample_fallback: bool = False,
    stale_if_error: bool = False,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
) -> FetchResult:
    cache_file: Path | None = Path(cache_path) if cache_path is not None else None
    remote = remote_url or os.getenv("NOAA_STATIONS_URL")
    cache_metadata = read_cache_metadata(cache_file) if cache_file is not None else {}

    if cache_file is not None and cache_file.exists() and not refresh:
        effective_ttl = max(cache_ttl, 0)
        age_seconds = _resolve_cache_age_seconds(cache_file, cache_metadata)
        source_match = _cache_matches_remote_source(cache_metadata, remote)
        if age_seconds <= effective_ttl and source_match:
            cached = load_stations(cache_file)
            if cached:
                return FetchResult(
                    stations=cached,
                    source="cache-fresh",
                    metadata={
                        "cache_age_seconds": age_seconds,
                        "cache_path": str(cache_file),
                        "cache_metadata": cache_metadata,
                    },
                )

    remote_token = token or os.getenv("NOAA_TOKEN")
    provider = token_provider
    if provider is None and token is None:
        env_provider = TokenProvider.from_env()
        provider = env_provider if env_provider.has_tokens() else None
    if remote:
        try:
            fetched, remote_meta = fetch_remote_stations(
                remote,
                token=remote_token,
                token_provider=provider,
            )
        except NoaaClientError as exc:
            if stale_if_error and cache_file is not None and cache_file.exists():
                stale_cached = load_stations(cache_file)
                if stale_cached:
                    return FetchResult(
                        stations=stale_cached,
                        source="cache-stale",
                        metadata={
                            "cache_path": str(cache_file),
                            "cache_age_seconds": _resolve_cache_age_seconds(cache_file, cache_metadata),
                            "cache_metadata": cache_metadata,
                            "warning": str(exc),
                            "remote_url": remote,
                        },
                    )
            if allow_sample_fallback:
                return FetchResult(
                    stations=[dict(station) for station in STATIONS],
                    source="sample-fallback",
                    metadata={"warning": str(exc), "remote_url": remote},
                )
            raise

        if fetched:
            if cache_file is not None:
                now_ts = int(time.time())
                cache_file.write_text(json.dumps(fetched, ensure_ascii=False, indent=2), encoding="utf-8")
                _write_cache_metadata(
                    cache_file,
                    fetched,
                    fetched_at=now_ts,
                    source_url=remote,
                    http_status=remote_meta.get("http_status"),
                    payload_shape=remote_meta.get("payload_shape"),
                    token_fingerprint=remote_meta.get("token_fingerprint"),
                    etag=remote_meta.get("etag"),
                    last_modified=remote_meta.get("last_modified"),
                )
            return FetchResult(
                stations=fetched,
                source="remote",
                metadata={"remote_url": remote, **remote_meta},
            )

        if allow_sample_fallback:
            return FetchResult(
                stations=[dict(station) for station in STATIONS],
                source="sample-fallback",
                metadata={"remote_url": remote, "warning": "Remote returned no stations"},
            )

        raise NoaaPayloadError("Remote returned no usable stations")

    if cache_file is not None and cache_file.exists():
        cached = load_stations(cache_file)
        if cached:
            return FetchResult(
                stations=cached,
                source="cache",
                metadata={
                    "cache_path": str(cache_file),
                    "cache_age_seconds": _resolve_cache_age_seconds(cache_file, cache_metadata),
                    "cache_metadata": cache_metadata,
                },
            )

    return FetchResult(
        stations=[dict(station) for station in STATIONS],
        source="sample-default",
        metadata={"warning": "No remote source configured"},
    )


def fetch_stations_with_cache(
    cache_path: str | Path | None = None,
    remote_url: str | None = None,
    cache_ttl: int = 3600,
    refresh: bool = False,
) -> list[StationRecord]:
    return fetch_stations_with_cache_details(
        cache_path=cache_path,
        remote_url=remote_url,
        cache_ttl=cache_ttl,
        refresh=refresh,
        allow_sample_fallback=True,
        stale_if_error=True,
    ).stations


def export_stations(
    stations: Sequence[StationRecord],
    output_json: str | Path | None = None,
    output_csv: str | Path | None = None,
    pretty: bool = False,
    noaa_like: bool = False,
) -> None:
    rows = list(stations)

    if output_json is not None:
        payload: Any
        if noaa_like:
            payload = to_noaa_like_payload(rows)
        else:
            payload = rows
        if pretty:
            Path(output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            Path(output_json).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if output_csv is not None:
        with Path(output_csv).open("w", encoding="utf-8", newline="") as handle:
            if rows:
                if noaa_like:
                    fieldnames = ["id", "name", "city", "country", "source"]
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(
                            {
                                "id": row.get("station_id"),
                                "name": row.get("name"),
                                "city": row.get("city"),
                                "country": row.get("country"),
                                "source": row.get("source", "local"),
                            }
                        )
                    return

                fieldnames = sorted({key for row in rows for key in row.keys()})
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)


def to_noaa_like_payload(stations: Sequence[StationRecord]) -> dict[str, Any]:
    rows = list(stations)
    return {
        "metadata": {"resultset": {"count": len(rows)}},
        "results": [
            {
                "id": row.get("station_id"),
                "name": row.get("name"),
                "city": row.get("city"),
                "country": row.get("country"),
                "source": row.get("source", "local"),
            }
            for row in rows
        ],
    }
