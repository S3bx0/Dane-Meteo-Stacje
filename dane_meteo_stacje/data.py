from __future__ import annotations

import calendar
import csv
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from random import SystemRandom
from threading import Lock
from typing import Any, TypedDict, cast
from urllib.parse import urlencode, urljoin, urlparse

import requests
from dotenv import dotenv_values
from typing_extensions import NotRequired

from . import __version__
from .countries import COUNTRY_CODE_MAP, country_to_fips_code, normalize_country_name
from .observability import log_event

_RANDOM = SystemRandom()
_MAX_REDIRECTS = 5
NOAA_API_BASE_URL = "https://www.ncei.noaa.gov/cdo-web/api/v2"
NOAA_ALLOWED_HOSTS = frozenset({"ncei.noaa.gov", "www.ncei.noaa.gov"})
NOAA_STATIONS_ENDPOINT = f"{NOAA_API_BASE_URL}/stations"
NOAA_DATA_ENDPOINT = f"{NOAA_API_BASE_URL}/data"
NOAA_DATATYPES_ENDPOINT = f"{NOAA_API_BASE_URL}/datatypes"
NOAA_PAGE_LIMIT = 1000
MONTH_NAMES = [calendar.month_name[index] for index in range(1, 13)]
CORE_TEMPERATURE_DATATYPES = ("TMIN", "TAVG", "TMAX")
TEMPERATURE_EXPORT_MODES = ("daily", "monthly", "extended")
TOKEN_ENV_NAMES = ("NOAA_API_TOKENS", "NOAA_TOKENS", "NOAA_TOKEN")
TOKEN_REQUEST_INTERVAL_SECONDS = 0.26


class NoaaClientError(Exception):
    """Base class for NOAA fetch errors."""


class NoaaAuthError(NoaaClientError):
    """Authentication or authorization failed."""


class NoaaRateLimitError(NoaaClientError):
    """Remote API rate limit hit."""


class NoaaNetworkError(NoaaClientError):
    """Remote API is unavailable or request failed."""


class NoaaTimeoutError(NoaaNetworkError):
    """Remote API did not complete within the request deadline."""


class NoaaPayloadError(NoaaClientError):
    """Remote API returned an invalid payload."""


def _is_noaa_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    return normalized in NOAA_ALLOWED_HOSTS


def _validate_noaa_https_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise NoaaNetworkError("Remote URL is invalid") from exc

    if parsed.scheme.lower() != "https":
        raise NoaaNetworkError("Remote URL must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise NoaaNetworkError("Remote URL must contain a host without credentials")
    if not _is_noaa_hostname(parsed.hostname):
        raise NoaaNetworkError("Remote URL host must be an approved NOAA NCEI server")

    try:
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for *_, sockaddr in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM):
            raw_address = sockaddr[0]
            if not isinstance(raw_address, str):
                raise NoaaNetworkError("Remote URL resolved to an unsupported address")
            addresses.add(ipaddress.ip_address(raw_address.split("%", 1)[0]))
    except (OSError, ValueError) as exc:
        raise NoaaNetworkError("Remote URL host could not be resolved") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise NoaaNetworkError("Remote URL must resolve only to public addresses")
    return url


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
    latitude: NotRequired[float]
    longitude: NotRequired[float]
    elevation: NotRequired[float]
    mindate: NotRequired[str]
    maxdate: NotRequired[str]
    datacoverage: NotRequired[float]
    quality: NotRequired[dict[str, Any]]
    source: NotRequired[str]
    notes: NotRequired[str]


def station_quality_summary(
    station: Mapping[str, Any],
    *,
    available_datatypes: Sequence[str] | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Score station suitability without requiring a bulk NOAA datatype scan.

    Catalogue-only scores use coverage, observation span and recency.  When
    ``available_datatypes`` is supplied, fifteen points are reserved for
    verified TMIN/TAVG/TMAX availability and the assessment becomes verified.
    """

    try:
        raw_coverage = float(station.get("datacoverage", 0.0))
    except (TypeError, ValueError):
        raw_coverage = 0.0
    coverage = min(1.0, max(0.0, raw_coverage / 100 if raw_coverage > 1 else raw_coverage))

    def parse_catalogue_date(key: str) -> date | None:
        raw_value = station.get(key)
        if not isinstance(raw_value, str):
            return None
        try:
            return date.fromisoformat(raw_value[:10])
        except ValueError:
            return None

    start_date = parse_catalogue_date("mindate")
    end_date = parse_catalogue_date("maxdate")
    if start_date is not None and end_date is not None and end_date >= start_date:
        period_years = round((end_date - start_date).days / 365.2425, 1)
    else:
        period_years = 0.0

    today = reference_date or date.today()
    recency_years = max(0, today.year - end_date.year) if end_date is not None else None
    if recency_years is None:
        recency_points = 0.0
    elif recency_years <= 2:
        recency_points = 10.0
    elif recency_years <= 5:
        recency_points = 7.0
    elif recency_years <= 10:
        recency_points = 3.0
    else:
        recency_points = 0.0

    verified = available_datatypes is not None
    normalized_datatypes = {
        str(datatype).strip().upper()
        for datatype in (available_datatypes or [])
        if str(datatype).strip()
    }
    if verified:
        coverage_points = coverage * 50
        period_points = min(period_years / 50, 1.0) * 25
        datatype_points = sum(5 for datatype in CORE_TEMPERATURE_DATATYPES if datatype in normalized_datatypes)
    else:
        coverage_points = coverage * 60
        period_points = min(period_years / 50, 1.0) * 30
        datatype_points = None

    score = round(coverage_points + period_points + recency_points + (datatype_points or 0))
    if score >= 75 and coverage >= 0.75 and period_years >= 20:
        grade = "good"
        label = "dobra"
    elif score >= 45 and coverage >= 0.4 and period_years >= 5:
        grade = "medium"
        label = "średnia"
    else:
        grade = "weak"
        label = "słaba"

    reasons = [
        f"kompletność katalogowa {coverage * 100:.1f}%",
        f"okres danych {period_years:.1f} lat",
        "aktualne dane" if recency_years is not None and recency_years <= 2 else "starsze lub nieznane dane końcowe",
    ]
    if verified:
        core_labels = [datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in normalized_datatypes]
        reasons.append(f"potwierdzone typy: {', '.join(core_labels) if core_labels else 'brak'}")

    return {
        "score": score,
        "grade": grade,
        "label": label,
        "assessment": "verified" if verified else "catalogue",
        "coverage_percent": round(coverage * 100, 1),
        "period_years": period_years,
        "recency_years": recency_years,
        "available_datatypes": [
            datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in normalized_datatypes
        ],
        "components": {
            "coverage": round(coverage_points, 1),
            "period": round(period_points, 1),
            "recency": round(recency_points, 1),
            "datatypes": datatype_points,
        },
        "reasons": reasons,
    }


def private_env_file_path() -> Path:
    """Return the per-user token file, outside a possibly shared project folder."""

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "Dane-Meteo-Stacje" / ".env"


def resolve_env_file() -> Path:
    """Choose explicit, private or legacy project configuration in that order."""

    configured = os.getenv("DANE_METEO_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()

    private_file = private_env_file_path()
    project_file = Path.cwd() / ".env"
    if private_file.is_file():
        private_values = dotenv_values(private_file)
        if any(
            isinstance(private_values.get(name), str) and str(private_values[name]).strip()
            for name in TOKEN_ENV_NAMES
        ):
            return private_file
    if project_file.is_file():
        return project_file
    return private_file


class TokenProvider:
    def __init__(
        self,
        tokens: Sequence[str],
        *,
        rate_limit_cooldown_seconds: int = 30,
        auth_quarantine_seconds: int = 300,
        request_interval_seconds: float = 0.0,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        cleaned = [token.strip() for token in tokens if token and token.strip()]
        self._tokens = cleaned
        self._cursor = 0
        self._rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self._auth_quarantine_seconds = auth_quarantine_seconds
        self._request_interval_seconds = max(float(request_interval_seconds), 0.0)
        self._now_fn = now_fn or time.time
        self._sleep_fn = sleep_fn or time.sleep
        self._blocked_until: dict[str, float] = {}
        self._next_request_at: dict[str, float] = {}
        self._usage = {token: 0 for token in cleaned}
        self._rate_limit_events = 0
        self._lock = Lock()

    @classmethod
    def configured_tokens(cls) -> list[str]:
        env_file = resolve_env_file()
        file_values = dotenv_values(env_file) if env_file.is_file() else {}

        def configured_value(name: str) -> str:
            process_value = os.getenv(name, "")
            if process_value.strip():
                return process_value
            file_value = file_values.get(name)
            return file_value if isinstance(file_value, str) else ""

        tokens: list[str] = []
        api_tokens_env = configured_value("NOAA_API_TOKENS")
        if api_tokens_env.strip():
            tokens.extend([token.strip() for token in api_tokens_env.split(",") if token.strip()])

        tokens_env = configured_value("NOAA_TOKENS")
        if tokens_env.strip():
            tokens.extend([token.strip() for token in tokens_env.split(",") if token.strip()])

        single_token = configured_value("NOAA_TOKEN").strip()
        if single_token and single_token not in tokens:
            tokens.append(single_token)
        # Keep order while removing duplicates.
        unique_tokens = list(dict.fromkeys(tokens))
        return unique_tokens

    @classmethod
    def from_env(cls) -> TokenProvider:
        return cls(cls.configured_tokens())

    def has_tokens(self) -> bool:
        with self._lock:
            return bool(self._tokens)

    def has_available_token(self) -> bool:
        with self._lock:
            now = float(self._now_fn())
            return any(self._blocked_until.get(token, 0.0) <= now for token in self._tokens)

    def acquire(self, *, wait_for_slot: bool = False, max_wait_seconds: float = 1.0) -> str | None:
        wait_deadline = time.monotonic() + max(max_wait_seconds, 0.0)
        while True:
            wait_seconds: float | None = None
            with self._lock:
                if not self._tokens:
                    return None

                now = float(self._now_fn())
                total = len(self._tokens)
                for step in range(total):
                    idx = (self._cursor + step) % total
                    token = self._tokens[idx]
                    if self._blocked_until.get(token, 0.0) > now:
                        continue
                    next_request = self._next_request_at.get(token, 0.0)
                    if next_request <= now:
                        self._cursor = (idx + 1) % total
                        self._usage[token] += 1
                        self._next_request_at[token] = now + self._request_interval_seconds
                        return token
                    token_wait = next_request - now
                    wait_seconds = token_wait if wait_seconds is None else min(wait_seconds, token_wait)

                if wait_seconds is None:
                    # Every token is quarantined because of authentication or HTTP 429.
                    return None

            if not wait_for_slot or time.monotonic() + wait_seconds > wait_deadline:
                return None
            self._sleep_fn(max(wait_seconds, 0.001))

    def mark_rate_limited(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._blocked_until[token] = float(self._now_fn()) + float(self._rate_limit_cooldown_seconds)
            self._rate_limit_events += 1

    def mark_auth_failed(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._blocked_until[token] = float(self._now_fn()) + float(self._auth_quarantine_seconds)

    def usage_summary(self) -> dict[str, int]:
        with self._lock:
            summary: dict[str, int] = {}
            for token, count in self._usage.items():
                masked = _mask_token(token)
                summary[masked] = summary.get(masked, 0) + count
            return summary

    @property
    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def rate_limit_events(self) -> int:
        with self._lock:
            return self._rate_limit_events


_SHARED_PROVIDER_LOCK = Lock()
_SHARED_PROVIDER_SIGNATURE: tuple[str, ...] = ()
_SHARED_PROVIDER: TokenProvider | None = None


def shared_env_token_provider() -> TokenProvider | None:
    """Keep cooldowns and fair rotation alive across local HTTP requests."""

    global _SHARED_PROVIDER, _SHARED_PROVIDER_SIGNATURE
    tokens = TokenProvider.configured_tokens()
    signature = tuple(tokens)
    with _SHARED_PROVIDER_LOCK:
        if _SHARED_PROVIDER is None or signature != _SHARED_PROVIDER_SIGNATURE:
            _SHARED_PROVIDER = TokenProvider(
                tokens,
                request_interval_seconds=TOKEN_REQUEST_INTERVAL_SECONDS,
            )
            _SHARED_PROVIDER_SIGNATURE = signature
        return _SHARED_PROVIDER if _SHARED_PROVIDER.has_tokens() else None


def _token_fingerprint(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return f"{token[:2]}***"
    return f"{token[:4]}...{token[-4:]}"


def _clone_station(station: StationRecord) -> StationRecord:
    return cast(StationRecord, dict(station))


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
        timeout: float = 10,
        max_retries_rate_limit: int = 2,
        max_retries_server_error: int = 2,
        backoff_seconds: float = 0.25,
        jitter_seconds: float = 0.05,
        user_agent: str = f"dane-meteo-stacje/{__version__}",
    ) -> None:
        self.timeout = timeout
        self.max_retries_rate_limit = max_retries_rate_limit
        self.max_retries_server_error = max_retries_server_error
        self.backoff_seconds = backoff_seconds
        self.jitter_seconds = max(jitter_seconds, 0.0)
        self.user_agent = user_agent
        self._session = requests.Session()

    def _sleep_with_backoff(self, attempt: int, deadline: float) -> None:
        base = self.backoff_seconds * (2**attempt)
        jitter = _RANDOM.uniform(0, self.jitter_seconds) if self.jitter_seconds > 0 else 0.0
        delay = base + jitter
        remaining = deadline - time.monotonic()
        if remaining <= delay:
            raise NoaaTimeoutError("Remote request deadline exceeded")
        time.sleep(delay)

    def fetch_json(self, url: str, token: str | None = None) -> tuple[Any, int, dict[str, str]]:
        current_url = _validate_noaa_https_url(url)
        deadline = time.monotonic() + self.timeout
        rate_retries_used = 0
        server_retries_used = 0
        redirects_followed = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NoaaTimeoutError("Remote request deadline exceeded")
            headers: dict[str, str] = {"User-Agent": self.user_agent}
            current_hostname = urlparse(current_url).hostname
            if token and current_hostname and _is_noaa_hostname(current_hostname):
                headers["token"] = token

            try:
                response = self._session.get(
                    current_url,
                    timeout=max(min(self.timeout, remaining), 0.001),
                    headers=headers,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                if server_retries_used < self.max_retries_server_error:
                    self._sleep_with_backoff(server_retries_used, deadline)
                    server_retries_used += 1
                    continue
                raise NoaaTimeoutError("Remote request deadline exceeded") from exc
            except requests.RequestException as exc:
                if server_retries_used < self.max_retries_server_error:
                    self._sleep_with_backoff(server_retries_used, deadline)
                    server_retries_used += 1
                    continue
                raise NoaaNetworkError(str(exc)) from exc

            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise NoaaNetworkError("Remote redirect is missing a Location header")
                redirects_followed += 1
                if redirects_followed > _MAX_REDIRECTS:
                    raise NoaaNetworkError("Remote URL exceeded the redirect limit")
                current_url = _validate_noaa_https_url(urljoin(current_url, location))
                continue
            if status in (401, 403):
                raise NoaaAuthError(f"HTTP {status}")
            if status == 429:
                if rate_retries_used < self.max_retries_rate_limit:
                    self._sleep_with_backoff(rate_retries_used, deadline)
                    rate_retries_used += 1
                    continue
                raise NoaaRateLimitError("HTTP 429")
            if status >= 500:
                if server_retries_used < self.max_retries_server_error:
                    self._sleep_with_backoff(server_retries_used, deadline)
                    server_retries_used += 1
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

    normalized_values = cast(dict[str, Any], normalized)
    for key in ("latitude", "longitude", "elevation", "datacoverage"):
        try:
            if item.get(key) is not None:
                normalized_values[key] = float(item[key])
        except (TypeError, ValueError):
            pass
    for key in ("mindate", "maxdate"):
        if isinstance(item.get(key), str) and str(item[key]).strip():
            normalized_values[key] = str(item[key])

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
    def _normalize_country_value(raw_value: Any) -> str | None:
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip()
        if not value:
            return None
        if len(value) == 2:
            return COUNTRY_CODE_MAP.get(value.upper(), value.upper())
        return value

    def _country_from_station_prefix(station_code: str) -> str | None:
        bare_station_code = station_code.split(":", 1)[-1]
        prefix = bare_station_code[:2].upper()
        return COUNTRY_CODE_MAP.get(prefix)

    country = item.get("country")
    normalized_country = _normalize_country_value(country)
    if normalized_country:
        return normalized_country

    location = item.get("location")
    if isinstance(location, dict):
        normalized_location_country = _normalize_country_value(location.get("country"))
        if normalized_location_country:
            return normalized_location_country

    from_prefix = _country_from_station_prefix(station_id)
    if from_prefix:
        return from_prefix
    return "Unknown"


def _extract_lat_lon(item: dict[str, Any]) -> tuple[float | None, float | None]:
    lat: Any = item.get("latitude")
    lon: Any = item.get("longitude")

    if (lat is None or lon is None) and isinstance(item.get("coordinates"), dict):
        coords = cast(dict[str, Any], item["coordinates"])
        lat = coords.get("latitude", lat)
        lon = coords.get("longitude", lon)

    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        return None, None

    return lat_f, lon_f


def _normalize_noaa_payload(payload: Any, *, stats: dict[str, int] | None = None) -> list[StationRecord]:
    quality = stats if stats is not None else {}
    quality.setdefault("items_total", 0)
    quality.setdefault("items_valid", 0)
    quality.setdefault("items_invalid", 0)
    quality.setdefault("invalid_not_object", 0)
    quality.setdefault("invalid_missing_id_or_name", 0)
    quality.setdefault("geo_valid", 0)
    quality.setdefault("geo_missing", 0)
    quality.setdefault("geo_out_of_range", 0)

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
        quality["items_total"] += 1
        if not isinstance(item, dict):
            quality["items_invalid"] += 1
            quality["invalid_not_object"] += 1
            continue
        station_id = item.get("id") or item.get("station_id")
        name = item.get("name") or item.get("station") or item.get("id")
        if (
            not station_id
            or not name
            or not str(station_id).strip()
            or not str(name).strip()
        ):
            quality["items_invalid"] += 1
            quality["invalid_missing_id_or_name"] += 1
            continue

        city_name = _extract_city_from_noaa_item(item, str(name))
        country_name = _infer_country_from_noaa_item(item, str(station_id))
        normalized_record: StationRecord = {
            "station_id": str(station_id),
            "city": city_name,
            "name": str(name),
            "country": country_name,
            "source": "NOAA",
            "notes": "Mapped from NOAA station payload",
        }

        lat, lon = _extract_lat_lon(item)
        if lat is None or lon is None:
            quality["geo_missing"] += 1
        elif -90 <= lat <= 90 and -180 <= lon <= 180:
            normalized_record["latitude"] = lat
            normalized_record["longitude"] = lon
            quality["geo_valid"] += 1
        else:
            quality["geo_out_of_range"] += 1

        for source_key in ("mindate", "maxdate"):
            source_value = item.get(source_key)
            if isinstance(source_value, str) and source_value.strip():
                normalized_record[source_key] = source_value

        elevation = item.get("elevation")
        try:
            if elevation is not None:
                normalized_record["elevation"] = float(elevation)
        except (TypeError, ValueError):
            pass

        datacoverage = item.get("datacoverage")
        try:
            if datacoverage is not None:
                normalized_record["datacoverage"] = float(datacoverage)
        except (TypeError, ValueError):
            pass

        normalized_records.append(normalized_record)
        quality["items_valid"] += 1
    return normalized_records


def load_stations(source: str | Path | None = None) -> list[StationRecord]:
    if source is None:
        return [_clone_station(station) for station in STATIONS]

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


def _resolve_token_provider(token: str | None, token_provider: TokenProvider | None) -> TokenProvider | None:
    provider = token_provider
    if provider is None:
        if token:
            provider = TokenProvider([token])
        else:
            provider = shared_env_token_provider()
    return provider


def _fetch_noaa_json(
    url: str,
    *,
    timeout: float = 10,
    token: str | None = None,
    client: NoaaClient | None = None,
    token_provider: TokenProvider | None = None,
) -> tuple[Any, int, dict[str, str], str | None]:
    noaa_client = client or NoaaClient(timeout=timeout)
    provider = _resolve_token_provider(token, token_provider)

    if provider is None:
        payload, http_status, response_meta = noaa_client.fetch_json(url, token=token)
        return payload, http_status, response_meta, token

    last_token_error: NoaaAuthError | NoaaRateLimitError | None = None
    for _ in range(max(provider.token_count, 1)):
        active_token = provider.acquire(wait_for_slot=True, max_wait_seconds=min(timeout, 1.0))
        if active_token is None:
            if last_token_error is not None:
                raise last_token_error
            raise NoaaAuthError("No healthy NOAA token available")
        try:
            payload, http_status, response_meta = noaa_client.fetch_json(url, token=active_token)
            return payload, http_status, response_meta, active_token
        except NoaaRateLimitError as exc:
            provider.mark_rate_limited(active_token)
            last_token_error = exc
        except NoaaAuthError as exc:
            provider.mark_auth_failed(active_token)
            last_token_error = exc

    if last_token_error is not None:
        raise last_token_error
    raise NoaaAuthError("No healthy NOAA token available")


def fetch_remote_stations(
    url: str,
    timeout: float = 10,
    token: str | None = None,
    client: NoaaClient | None = None,
    token_provider: TokenProvider | None = None,
) -> tuple[list[StationRecord], dict[str, Any]]:
    payload, http_status, response_meta, active_token = _fetch_noaa_json(
        url,
        timeout=timeout,
        token=token,
        client=client,
        token_provider=token_provider,
    )

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("stations"), list):
        records = payload["stations"]
    else:
        normalization_stats: dict[str, int] = {}
        records = _normalize_noaa_payload(payload, stats=normalization_stats)
        if records:
            return records, {
                "http_status": http_status,
                "payload_shape": "noaa-results",
                "token_fingerprint": _token_fingerprint(active_token),
                "etag": response_meta.get("etag") or None,
                "last_modified": response_meta.get("last_modified") or None,
                "normalization": normalization_stats,
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


def _write_json_atomic(path: str | Path, payload: Any, *, indent: int | None = 2) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def read_cache_metadata(cache_path: str | Path) -> dict[str, Any]:
    metadata_path = _cache_metadata_path(cache_path)
    if not metadata_path.exists():
        return {}

    try:
        parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return cast(dict[str, Any], parsed)
        return {}
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

    _write_json_atomic(metadata_path, metadata)


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


def _require_token_provider(token: str | None, token_provider: TokenProvider | None) -> TokenProvider:
    provider = _resolve_token_provider(token, token_provider)
    if provider is None or not provider.has_tokens():
        raise NoaaAuthError(
            "NOAA token is not configured; set NOAA_API_TOKENS, NOAA_TOKENS or NOAA_TOKEN"
        )
    return provider


def _resultset_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    resultset = metadata.get("resultset")
    if not isinstance(resultset, dict):
        return None
    try:
        return int(resultset["count"])
    except (KeyError, TypeError, ValueError):
        return None


def _read_country_station_cache(
    cache_path: Path,
    canonical_country: str,
    *,
    cache_ttl: int,
    allow_stale: bool = False,
) -> FetchResult | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("country") != canonical_country:
            return None
        fetched_at = int(payload["fetched_at"])
        age_seconds = max(int(time.time()) - fetched_at, 0)
        if not allow_stale and age_seconds > max(cache_ttl, 0):
            return None
        raw_stations = payload.get("stations")
        if not isinstance(raw_stations, list):
            return None
        stations = [station for item in raw_stations if (station := _normalize_station(item)) is not None]
        if not stations:
            return None
        return FetchResult(
            stations=stations,
            source="noaa-country-cache-stale" if allow_stale else "noaa-country-cache",
            metadata={
                "country": canonical_country,
                "country_code": payload.get("country_code"),
                "location_id": payload.get("location_id"),
                "returned_count": len(stations),
                "cache_age_seconds": age_seconds,
                "cache_path": str(cache_path),
            },
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def fetch_stations_for_country(
    country: str,
    *,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
    timeout: float = 15,
    page_limit: int = NOAA_PAGE_LIMIT,
    max_pages: int = 200,
    client: NoaaClient | None = None,
    cache_path: str | Path | None = None,
    cache_ttl: int = 24 * 60 * 60,
    refresh: bool = False,
    stale_if_error: bool = True,
) -> FetchResult:
    """Fetch every GHCND station for a country, following NOAA pagination."""

    canonical_country = normalize_country_name(country)
    country_code = country_to_fips_code(canonical_country)
    country_cache = Path(cache_path) if cache_path is not None else None
    if country_cache is not None and country_cache.is_file() and not refresh:
        cached = _read_country_station_cache(
            country_cache,
            canonical_country,
            cache_ttl=cache_ttl,
        )
        if cached is not None:
            return cached

    provider = _require_token_provider(token, token_provider)
    noaa_client = client or NoaaClient(timeout=timeout, max_retries_rate_limit=0)
    effective_limit = min(max(int(page_limit), 1), NOAA_PAGE_LIMIT)
    offset = 1
    pages = 0
    total_count: int | None = None
    stations_by_id: dict[str, StationRecord] = {}
    normalization_totals: dict[str, int] = {}
    operation_deadline = time.monotonic() + timeout

    try:
        while pages < max_pages:
            remaining_timeout = operation_deadline - time.monotonic()
            if remaining_timeout <= 0:
                raise NoaaTimeoutError("NOAA station search deadline exceeded")
            if isinstance(noaa_client, NoaaClient):
                noaa_client.timeout = remaining_timeout
            query = urlencode(
                [
                    ("datasetid", "GHCND"),
                    ("locationid", f"FIPS:{country_code}"),
                    ("datatypeid", "TMAX"),
                    ("datatypeid", "TMIN"),
                    ("limit", str(effective_limit)),
                    ("offset", str(offset)),
                    ("includemetadata", "true"),
                ]
            )
            url = f"{NOAA_STATIONS_ENDPOINT}?{query}"
            payload, _, _, _ = _fetch_noaa_json(
                url,
                timeout=remaining_timeout,
                client=noaa_client,
                token_provider=provider,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise NoaaPayloadError("NOAA stations response does not contain a results array")

            raw_results = cast(list[Any], payload["results"])
            if total_count is None:
                total_count = _resultset_count(payload)

            page_stats: dict[str, int] = {}
            for station in _normalize_noaa_payload(payload, stats=page_stats):
                station["country"] = canonical_country
                stations_by_id[station["station_id"]] = station
            for key, value in page_stats.items():
                normalization_totals[key] = normalization_totals.get(key, 0) + value

            pages += 1
            received = len(raw_results)
            if received == 0:
                break
            offset += received
            if total_count is not None and offset > total_count:
                break
            if received < effective_limit:
                break
        else:
            raise NoaaPayloadError(f"NOAA station pagination exceeded {max_pages} pages")
    except NoaaClientError:
        if country_cache is not None and country_cache.is_file() and stale_if_error:
            stale = _read_country_station_cache(
                country_cache,
                canonical_country,
                cache_ttl=cache_ttl,
                allow_stale=True,
            )
            if stale is not None:
                return stale
        raise

    stations = sorted(
        stations_by_id.values(),
        key=lambda station: (str(station.get("city", "")).casefold(), station["station_id"]),
    )
    result = FetchResult(
        stations=stations,
        source="noaa-country",
        metadata={
            "country": canonical_country,
            "country_code": country_code,
            "location_id": f"FIPS:{country_code}",
            "pages": pages,
            "reported_count": total_count,
            "returned_count": len(stations),
            "normalization": normalization_totals,
        },
    )
    if country_cache is not None and stations:
        _write_json_atomic(
            country_cache,
            {
                "schema_version": 1,
                "fetched_at": int(time.time()),
                "country": canonical_country,
                "country_code": country_code,
                "location_id": f"FIPS:{country_code}",
                "stations": stations,
            },
        )
    return result


def _normalize_ghcnd_station_id(station_id: str) -> str:
    normalized = station_id.strip().upper()
    if normalized.startswith("GHCND:"):
        normalized = normalized.split(":", 1)[1]
    if not re.fullmatch(r"[A-Z0-9]{11}", normalized):
        raise ValueError("station_id must be an 11-character GHCND identifier")
    return normalized


def _monthly_temperatures_from_records(year: int, records: list[dict[str, Any]]) -> list[float | None]:
    values: dict[tuple[int, str], list[float]] = {}
    for record in records:
        raw_date = record.get("date")
        datatype = str(record.get("datatype", "")).upper()
        if not isinstance(raw_date, str) or datatype not in {"TMAX", "TMIN"}:
            continue
        attributes = record.get("attributes")
        if isinstance(attributes, str):
            attribute_parts = attributes.split(",")
            if len(attribute_parts) > 1 and attribute_parts[1].strip():
                # NOAA's second attribute is QFLAG; non-empty values failed quality control.
                continue
        try:
            record_year = int(raw_date[:4])
            month = int(raw_date[5:7])
            value = float(record["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if record_year != year or not 1 <= month <= 12:
            continue
        values.setdefault((month, datatype), []).append(value)

    monthly: list[float | None] = []
    for month in range(1, 13):
        datatype_means: list[float] = []
        for datatype in ("TMAX", "TMIN"):
            observations = values.get((month, datatype), [])
            if observations:
                datatype_means.append(sum(observations) / len(observations))
        if datatype_means:
            monthly.append(round(sum(datatype_means) / len(datatype_means), 2))
        else:
            monthly.append(None)
    return monthly


def _fetch_temperature_year(
    year: int,
    station_id: str,
    *,
    token_provider: TokenProvider,
    timeout: float,
    page_limit: int,
) -> tuple[list[float | None], str]:
    client = NoaaClient(timeout=timeout, max_retries_rate_limit=0)
    offset = 1
    records: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        query = urlencode(
            [
                ("datasetid", "GHCND"),
                ("stationid", f"GHCND:{station_id}"),
                ("startdate", f"{year}-01-01"),
                ("enddate", f"{year}-12-31"),
                ("datatypeid", "TMAX"),
                ("datatypeid", "TMIN"),
                ("units", "metric"),
                ("limit", str(page_limit)),
                ("offset", str(offset)),
                ("includemetadata", "true"),
            ]
        )
        payload, _, _, _ = _fetch_noaa_json(
            f"{NOAA_DATA_ENDPOINT}?{query}",
            timeout=timeout,
            client=client,
            token_provider=token_provider,
        )
        if not isinstance(payload, dict):
            raise NoaaPayloadError("NOAA temperature response is not an object")
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise NoaaPayloadError("NOAA temperature response does not contain a results array")
        if total_count is None:
            total_count = _resultset_count(payload)
        records.extend(item for item in raw_results if isinstance(item, dict))

        received = len(raw_results)
        if received == 0:
            break
        offset += received
        if total_count is not None and offset > total_count:
            break
        if received < page_limit:
            break

    monthly = _monthly_temperatures_from_records(year, records)
    return monthly, "OK" if any(value is not None for value in monthly) else "No temperature data"


def _temperature_year_cache_path(cache_dir: Path, station_id: str, year: int) -> Path:
    safe_station_id = os.path.basename(station_id)
    if safe_station_id != station_id or not re.fullmatch(r"[A-Z0-9]{11}", safe_station_id):
        raise ValueError("station_id must be an 11-character GHCND identifier")
    return cache_dir / safe_station_id / f"{year}.json"


def _read_temperature_year_cache(
    cache_path: Path,
    station_id: str,
    year: int,
    *,
    cache_ttl_seconds: int,
) -> tuple[list[float | None], str] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = int(payload["fetched_at"])
        if int(time.time()) - fetched_at > max(cache_ttl_seconds, 0):
            return None
        if payload.get("station_id") != station_id or int(payload.get("year")) != year:
            return None
        monthly = payload.get("monthly")
        if not isinstance(monthly, list) or len(monthly) != 12:
            return None
        normalized = [float(value) if isinstance(value, (int, float)) else None for value in monthly]
        return normalized, str(payload.get("message") or "OK")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_temperature_year_cache(
    cache_path: Path,
    station_id: str,
    year: int,
    monthly: list[float | None],
    message: str,
) -> None:
    _write_json_atomic(
        cache_path,
        {
            "schema_version": 1,
            "fetched_at": int(time.time()),
            "station_id": station_id,
            "year": year,
            "monthly": monthly,
            "message": message,
        },
    )


def fetch_monthly_temperature_matrix(
    station_id: str,
    start_year: int,
    end_year: int,
    *,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
    timeout: float = 30,
    concurrency: int = 4,
    max_attempts: int = 2,
    cache_dir: str | Path | None = None,
    cache_ttl_seconds: int = 30 * 24 * 60 * 60,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the JSON matrix consumed by the Heatmapa application."""

    normalized_station_id = _normalize_ghcnd_station_id(station_id)
    current_year = time.gmtime().tm_year
    if start_year < 1763 or end_year > current_year:
        raise ValueError(f"year range must stay between 1763 and {current_year}")
    if start_year > end_year:
        raise ValueError("start_year must be less than or equal to end_year")
    if end_year - start_year > 150:
        raise ValueError("year range cannot exceed 151 years")

    requested_years = list(range(start_year, end_year + 1))
    year_to_monthly: dict[int, list[float | None]] = {}
    missing_report: dict[int, str] = {}
    temperature_cache_dir = Path(cache_dir) if cache_dir is not None else None
    remaining: list[int] = []
    if temperature_cache_dir is None or refresh:
        remaining = requested_years.copy()
    else:
        for year in requested_years:
            year_ttl = min(cache_ttl_seconds, 6 * 60 * 60) if year == current_year else cache_ttl_seconds
            cached = _read_temperature_year_cache(
                _temperature_year_cache_path(temperature_cache_dir, normalized_station_id, year),
                normalized_station_id,
                year,
                cache_ttl_seconds=year_ttl,
            )
            if cached is None:
                remaining.append(year)
                continue
            monthly, message = cached
            year_to_monthly[year] = monthly
            if not any(value is not None for value in monthly):
                missing_report[year] = message

    provider = _require_token_provider(token, token_provider) if remaining else None
    adaptive_history: list[dict[str, Any]] = []
    workers = min(max(int(concurrency), 1), 8, max(len(remaining), 1))
    technical_errors: dict[int, NoaaClientError] = {}

    for attempt in range(1, max(max_attempts, 1) + 1):
        if not remaining:
            break
        attempt_years = remaining
        remaining = []
        if provider is None:
            break
        rate_limit_before = provider.rate_limit_events
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="noaa-temperature") as executor:
            futures = {
                executor.submit(
                    _fetch_temperature_year,
                    year,
                    normalized_station_id,
                    token_provider=provider,
                    timeout=timeout,
                    page_limit=NOAA_PAGE_LIMIT,
                ): year
                for year in attempt_years
            }
            for future in as_completed(futures):
                year = futures[future]
                try:
                    monthly, message = future.result()
                except NoaaClientError as exc:
                    remaining.append(year)
                    technical_errors[year] = exc
                    missing_report[year] = str(exc)
                    continue
                year_to_monthly[year] = monthly
                technical_errors.pop(year, None)
                if any(value is not None for value in monthly):
                    missing_report.pop(year, None)
                else:
                    missing_report[year] = message
                if temperature_cache_dir is not None:
                    _write_temperature_year_cache(
                        _temperature_year_cache_path(
                            temperature_cache_dir,
                            normalized_station_id,
                            year,
                        ),
                        normalized_station_id,
                        year,
                        monthly,
                        message,
                    )

        rate_limit_delta = provider.rate_limit_events - rate_limit_before
        next_workers = max(1, workers - 1) if rate_limit_delta or remaining else min(8, workers + 1)
        adaptive_history.append(
            {
                "attempt": attempt,
                "strategy": "balanced",
                "used_concurrency": workers,
                "next_concurrency": next_workers,
                "rate_limit_events": rate_limit_delta,
                "failed_years": len(remaining),
                "years_processed": len(attempt_years),
                "years_remaining": len(remaining),
            }
        )
        workers = next_workers

    if technical_errors and not year_to_monthly:
        # A total NOAA failure must be an HTTP/API error, not a plausible all-null data file.
        for error_type in (NoaaAuthError, NoaaRateLimitError, NoaaTimeoutError, NoaaNetworkError):
            matching = next(
                (error for error in technical_errors.values() if isinstance(error, error_type)),
                None,
            )
            if matching is not None:
                raise matching
        raise next(iter(technical_errors.values()))

    for year in requested_years:
        year_to_monthly.setdefault(year, [None] * 12)
        if not any(value is not None for value in year_to_monthly[year]):
            missing_report.setdefault(year, "No temperature data")

    final_missing_years = [
        year for year in requested_years if not any(value is not None for value in year_to_monthly[year])
    ]
    return {
        "station_id": normalized_station_id,
        "years": requested_years,
        "months": MONTH_NAMES.copy(),
        "temperatures": [year_to_monthly[year] for year in requested_years],
        "final_missing_years": final_missing_years,
        "missing_data_report": {str(year): missing_report[year] for year in sorted(missing_report)},
        "token_usage": provider.usage_summary() if provider is not None else {},
        "adaptive_history": adaptive_history,
    }


def _temperature_method_metadata() -> dict[str, dict[str, Any]]:
    """Describe reported and derived temperature fields without ambiguity."""

    return {
        "TMIN": {
            "field": "tmin",
            "origin": "NOAA GHCND",
            "method": "reported_daily_minimum",
            "description": "Daily minimum temperature reported by NOAA.",
        },
        "TAVG": {
            "field": "tavg",
            "origin": "NOAA GHCND",
            "method": "reported_daily_average_source_dependent",
            "source_dependent": True,
            "description": (
                "Daily average temperature reported by NOAA. Its observation or calculation method "
                "can depend on the contributing source and is not assumed to equal TAXN."
            ),
        },
        "TAXN": {
            "field": "taxn",
            "origin": "calculated_by_dane_meteo_stacje",
            "method": "daily_midrange",
            "formula": "(TMAX + TMIN) / 2",
            "requires": ["TMAX", "TMIN"],
            "description": "Calculated only for days with both valid TMAX and TMIN.",
        },
        "TMAX": {
            "field": "tmax",
            "origin": "NOAA GHCND",
            "method": "reported_daily_maximum",
            "description": "Daily maximum temperature reported by NOAA.",
        },
        "AMPLITUDE": {
            "field": "amplitude",
            "origin": "calculated_by_dane_meteo_stacje",
            "method": "daily_temperature_range",
            "formula": "TMAX - TMIN",
            "requires": ["TMAX", "TMIN"],
            "description": "Calculated only for days with both valid TMAX and TMIN.",
        },
    }


def fetch_station_temperature_capabilities(
    station_id: str,
    *,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
    timeout: float = 15,
    client: NoaaClient | None = None,
) -> dict[str, Any]:
    """Return NOAA temperature datatypes advertised for a selected GHCND station."""

    normalized_station_id = _normalize_ghcnd_station_id(station_id)
    provider = _require_token_provider(token, token_provider)
    query = urlencode(
        [
            ("datasetid", "GHCND"),
            ("stationid", f"GHCND:{normalized_station_id}"),
            ("datacategoryid", "TEMP"),
            ("limit", str(NOAA_PAGE_LIMIT)),
            ("includemetadata", "true"),
        ]
    )
    payload, _, _, _ = _fetch_noaa_json(
        f"{NOAA_DATATYPES_ENDPOINT}?{query}",
        timeout=timeout,
        client=client or NoaaClient(timeout=timeout),
        token_provider=provider,
    )
    if not isinstance(payload, dict):
        raise NoaaPayloadError("NOAA datatype response is not an object")
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise NoaaPayloadError("NOAA datatype response does not contain a results array")

    details: list[dict[str, Any]] = []
    available_ids: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        datatype = str(item.get("id", "")).strip().upper()
        if not datatype:
            continue
        available_ids.add(datatype)
        details.append(
            {
                key: item[key]
                for key in ("id", "name", "mindate", "maxdate", "datacoverage")
                if key in item
            }
        )

    core = [datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in available_ids]
    taxn_available = {"TMIN", "TMAX"}.issubset(available_ids)
    has_temperature = bool(core)
    return {
        "station_id": normalized_station_id,
        "dataset_id": "GHCND",
        "available_datatypes": sorted(available_ids),
        "core_temperature_datatypes": core,
        "datatype_details": sorted(details, key=lambda item: str(item.get("id", ""))),
        "derived_datatypes": {
            "TAXN": taxn_available,
            "AMPLITUDE": taxn_available,
        },
        "export_modes": {
            "heatmap": bool({"TMIN", "TMAX"}.intersection(available_ids)),
            "daily": has_temperature,
            "monthly": has_temperature,
            "extended": has_temperature,
        },
        "temperature_methods": _temperature_method_metadata(),
    }


def _fetch_temperature_records_year(
    year: int,
    station_id: str,
    datatypes: Sequence[str],
    *,
    token_provider: TokenProvider,
    timeout: float,
    page_limit: int,
) -> tuple[list[dict[str, Any]], str]:
    client = NoaaClient(timeout=timeout, max_retries_rate_limit=0)
    offset = 1
    records: list[dict[str, Any]] = []
    total_count: int | None = None
    query_datatypes = [datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in datatypes]

    while True:
        query_items = [
            ("datasetid", "GHCND"),
            ("stationid", f"GHCND:{station_id}"),
            ("startdate", f"{year}-01-01"),
            ("enddate", f"{year}-12-31"),
        ]
        query_items.extend(("datatypeid", datatype) for datatype in query_datatypes)
        query_items.extend(
            [
                ("units", "metric"),
                ("limit", str(page_limit)),
                ("offset", str(offset)),
                ("includemetadata", "true"),
            ]
        )
        payload, _, _, _ = _fetch_noaa_json(
            f"{NOAA_DATA_ENDPOINT}?{urlencode(query_items)}",
            timeout=timeout,
            client=client,
            token_provider=token_provider,
        )
        if not isinstance(payload, dict):
            raise NoaaPayloadError("NOAA temperature response is not an object")
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise NoaaPayloadError("NOAA temperature response does not contain a results array")
        if total_count is None:
            total_count = _resultset_count(payload)
        records.extend(item for item in raw_results if isinstance(item, dict))

        received = len(raw_results)
        if received == 0:
            break
        offset += received
        if total_count is not None and offset > total_count:
            break
        if received < page_limit:
            break

    return records, "OK" if records else "No temperature data"


def _temperature_records_cache_path(cache_dir: Path, station_id: str, year: int) -> Path:
    safe_station_id = os.path.basename(station_id)
    if safe_station_id != station_id or not re.fullmatch(r"[A-Z0-9]{11}", safe_station_id):
        raise ValueError("station_id must be an 11-character GHCND identifier")
    return cache_dir / "observations" / safe_station_id / f"{year}.json"


def _read_temperature_records_cache(
    cache_path: Path,
    station_id: str,
    year: int,
    datatypes: Sequence[str],
    *,
    cache_ttl_seconds: int,
) -> tuple[list[dict[str, Any]], str] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = int(payload["fetched_at"])
        if int(time.time()) - fetched_at > max(cache_ttl_seconds, 0):
            return None
        if payload.get("station_id") != station_id or int(payload.get("year")) != year:
            return None
        cached_datatypes = {str(value).upper() for value in payload.get("datatypes", [])}
        if not set(datatypes).issubset(cached_datatypes):
            return None
        records = payload.get("records")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            return None
        return cast(list[dict[str, Any]], records), str(payload.get("message") or "OK")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_temperature_records_cache(
    cache_path: Path,
    station_id: str,
    year: int,
    datatypes: Sequence[str],
    records: list[dict[str, Any]],
    message: str,
) -> None:
    _write_json_atomic(
        cache_path,
        {
            "schema_version": 1,
            "fetched_at": int(time.time()),
            "station_id": station_id,
            "year": year,
            "datatypes": list(datatypes),
            "records": records,
            "message": message,
        },
    )


def _validate_temperature_year_range(start_year: int, end_year: int) -> None:
    current_year = time.gmtime().tm_year
    if start_year < 1763 or end_year > current_year:
        raise ValueError(f"year range must stay between 1763 and {current_year}")
    if start_year > end_year:
        raise ValueError("start_year must be less than or equal to end_year")
    if end_year - start_year > 150:
        raise ValueError("year range cannot exceed 151 years")


def _fetch_temperature_record_range(
    station_id: str,
    start_year: int,
    end_year: int,
    datatypes: Sequence[str],
    *,
    token: str | None,
    token_provider: TokenProvider | None,
    timeout: float,
    concurrency: int,
    max_attempts: int,
    cache_dir: str | Path | None,
    cache_ttl_seconds: int,
    refresh: bool,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str], dict[str, int], list[dict[str, Any]]]:
    requested_years = list(range(start_year, end_year + 1))
    normalized_datatypes = tuple(
        datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in {value.upper() for value in datatypes}
    )
    if not normalized_datatypes:
        raise ValueError("at least one of TMIN, TAVG or TMAX is required")

    observation_cache_dir = Path(cache_dir) if cache_dir is not None else None
    year_records: dict[int, list[dict[str, Any]]] = {}
    missing_report: dict[int, str] = {}
    remaining: list[int] = []
    current_year = time.gmtime().tm_year
    if observation_cache_dir is None or refresh:
        remaining = requested_years.copy()
    else:
        for year in requested_years:
            year_ttl = min(cache_ttl_seconds, 6 * 60 * 60) if year == current_year else cache_ttl_seconds
            cached = _read_temperature_records_cache(
                _temperature_records_cache_path(observation_cache_dir, station_id, year),
                station_id,
                year,
                normalized_datatypes,
                cache_ttl_seconds=year_ttl,
            )
            if cached is None:
                remaining.append(year)
                continue
            records, message = cached
            year_records[year] = records
            if not records:
                missing_report[year] = message

    provider = _require_token_provider(token, token_provider) if remaining else None
    adaptive_history: list[dict[str, Any]] = []
    workers = min(max(int(concurrency), 1), 8, max(len(remaining), 1))
    technical_errors: dict[int, NoaaClientError] = {}

    for attempt in range(1, max(max_attempts, 1) + 1):
        if not remaining or provider is None:
            break
        attempt_years = remaining
        remaining = []
        rate_limit_before = provider.rate_limit_events
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="noaa-temperature-export") as executor:
            futures = {
                executor.submit(
                    _fetch_temperature_records_year,
                    year,
                    station_id,
                    normalized_datatypes,
                    token_provider=provider,
                    timeout=timeout,
                    page_limit=NOAA_PAGE_LIMIT,
                ): year
                for year in attempt_years
            }
            for future in as_completed(futures):
                year = futures[future]
                try:
                    records, message = future.result()
                except NoaaClientError as exc:
                    remaining.append(year)
                    technical_errors[year] = exc
                    missing_report[year] = str(exc)
                    continue
                year_records[year] = records
                technical_errors.pop(year, None)
                if records:
                    missing_report.pop(year, None)
                else:
                    missing_report[year] = message
                if observation_cache_dir is not None:
                    _write_temperature_records_cache(
                        _temperature_records_cache_path(observation_cache_dir, station_id, year),
                        station_id,
                        year,
                        normalized_datatypes,
                        records,
                        message,
                    )

        rate_limit_delta = provider.rate_limit_events - rate_limit_before
        next_workers = max(1, workers - 1) if rate_limit_delta or remaining else min(8, workers + 1)
        adaptive_history.append(
            {
                "attempt": attempt,
                "strategy": "balanced",
                "used_concurrency": workers,
                "next_concurrency": next_workers,
                "rate_limit_events": rate_limit_delta,
                "failed_years": len(remaining),
                "years_processed": len(attempt_years),
                "years_remaining": len(remaining),
            }
        )
        workers = next_workers

    if technical_errors and not year_records:
        for error_type in (NoaaAuthError, NoaaRateLimitError, NoaaTimeoutError, NoaaNetworkError):
            matching = next(
                (error for error in technical_errors.values() if isinstance(error, error_type)),
                None,
            )
            if matching is not None:
                raise matching
        raise next(iter(technical_errors.values()))

    for year in requested_years:
        year_records.setdefault(year, [])
        if not year_records[year]:
            missing_report.setdefault(year, "No temperature data")
    return (
        year_records,
        missing_report,
        provider.usage_summary() if provider is not None else {},
        adaptive_history,
    )


def _attribute_metadata(attributes: Any) -> dict[str, str]:
    parts = attributes.split(",") if isinstance(attributes, str) else []
    parts.extend([""] * (4 - len(parts)))
    return {
        "measurement_flag": parts[0].strip(),
        "quality_flag": parts[1].strip(),
        "source_flag": parts[2].strip(),
        "observation_time": parts[3].strip(),
    }


def _daily_temperature_rows(
    year: int,
    records: list[dict[str, Any]],
    *,
    final_day: date,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    values: dict[date, dict[str, list[float]]] = {}
    attributes_by_day: dict[date, dict[str, dict[str, str]]] = {}
    quality = {
        "records_received": len(records),
        "records_used": 0,
        "records_rejected_quality": 0,
        "records_rejected_invalid": 0,
    }
    for record in records:
        datatype = str(record.get("datatype", "")).upper()
        raw_date = record.get("date")
        if datatype not in CORE_TEMPERATURE_DATATYPES or not isinstance(raw_date, str):
            quality["records_rejected_invalid"] += 1
            continue
        attribute_metadata = _attribute_metadata(record.get("attributes"))
        if attribute_metadata["quality_flag"]:
            quality["records_rejected_quality"] += 1
            continue
        try:
            day_date = date.fromisoformat(raw_date[:10])
            value = float(record["value"])
        except (KeyError, TypeError, ValueError):
            quality["records_rejected_invalid"] += 1
            continue
        if day_date.year != year or day_date > final_day:
            quality["records_rejected_invalid"] += 1
            continue
        values.setdefault(day_date, {}).setdefault(datatype, []).append(value)
        attributes_by_day.setdefault(day_date, {})[datatype] = attribute_metadata
        quality["records_used"] += 1

    first_day = date(year, 1, 1)
    rows: list[dict[str, Any]] = []
    day_date = first_day
    while day_date <= final_day:
        day_values = values.get(day_date, {})

        tmin = _mean(day_values.get("TMIN", []))
        tavg = _mean(day_values.get("TAVG", []))
        tmax = _mean(day_values.get("TMAX", []))
        taxn = round((tmax + tmin) / 2, 2) if tmin is not None and tmax is not None else None
        amplitude = round(tmax - tmin, 2) if tmin is not None and tmax is not None else None
        row: dict[str, Any] = {
            "date": day_date.isoformat(),
            "tmin": tmin,
            "tavg": tavg,
            "taxn": taxn,
            "tmax": tmax,
            "amplitude": amplitude,
        }
        day_attributes = attributes_by_day.get(day_date)
        if day_attributes:
            row["attributes"] = day_attributes
        rows.append(row)
        day_date += timedelta(days=1)
    return rows, quality


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _completeness(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    expected = len(rows)
    result: dict[str, Any] = {"expected_days": expected}
    for field in ("tmin", "tavg", "taxn", "tmax", "amplitude"):
        observed = sum(1 for row in rows if isinstance(row.get(field), (int, float)))
        result[field] = {
            "observed_days": observed,
            "missing_days": expected - observed,
            "percent": round(observed * 100 / expected, 2) if expected else 0.0,
        }
    return result


def _monthly_temperature_export(
    station_id: str,
    years: list[int],
    rows: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, Any]:
    by_month: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        row_date = date.fromisoformat(str(row["date"]))
        by_month.setdefault((row_date.year, row_date.month), []).append(row)

    field_map = {
        "TMIN": "tmin",
        "TAVG": "tavg",
        "TAXN": "taxn",
        "TMAX": "tmax",
        "AMPLITUDE": "amplitude",
    }
    matrices: dict[str, list[list[float | None]]] = {datatype: [] for datatype in field_map}
    completeness_matrices: dict[str, list[list[float]]] = {datatype: [] for datatype in field_map}
    expected_days: list[list[int]] = []
    for year in years:
        expected_year: list[int] = []
        values_year: dict[str, list[float | None]] = {datatype: [] for datatype in field_map}
        completeness_year: dict[str, list[float]] = {datatype: [] for datatype in field_map}
        for month in range(1, 13):
            month_rows = by_month.get((year, month), [])
            expected_year.append(len(month_rows))
            for datatype, field in field_map.items():
                values = [float(row[field]) for row in month_rows if isinstance(row.get(field), (int, float))]
                values_year[datatype].append(_mean(values))
                completeness_year[datatype].append(
                    round(len(values) * 100 / len(month_rows), 2) if month_rows else 0.0
                )
        expected_days.append(expected_year)
        for datatype in field_map:
            matrices[datatype].append(values_year[datatype])
            completeness_matrices[datatype].append(completeness_year[datatype])

    return {
        **base,
        "export_type": "monthly",
        "station_id": station_id,
        "years": years,
        "months": MONTH_NAMES.copy(),
        "temperatures": matrices,
        "completeness": {
            "expected_days": expected_days,
            "percent": completeness_matrices,
        },
    }


def _statistic(values: Sequence[float], expected_days: int) -> dict[str, Any]:
    return {
        "count": len(values),
        "completeness_percent": round(len(values) * 100 / expected_days, 2) if expected_days else 0.0,
        "mean": _mean(values),
        "minimum": round(min(values), 2) if values else None,
        "maximum": round(max(values), 2) if values else None,
    }


def _extended_temperature_export(
    station_id: str,
    rows: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, Any]:
    by_month: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        row_date = date.fromisoformat(str(row["date"]))
        by_month.setdefault((row_date.year, row_date.month), []).append(row)

    monthly_statistics: list[dict[str, Any]] = []
    for (year, month), month_rows in sorted(by_month.items()):
        expected = len(month_rows)
        statistics: dict[str, Any] = {}
        for datatype, field in (
            ("TMIN", "tmin"),
            ("TAVG", "tavg"),
            ("TAXN", "taxn"),
            ("TMAX", "tmax"),
            ("AMPLITUDE", "amplitude"),
        ):
            values = [float(row[field]) for row in month_rows if isinstance(row.get(field), (int, float))]
            statistics[datatype] = _statistic(values, expected)

        differences = [
            float(row["tavg"]) - float(row["taxn"])
            for row in month_rows
            if isinstance(row.get("tavg"), (int, float)) and isinstance(row.get("taxn"), (int, float))
        ]
        monthly_statistics.append(
            {
                "year": year,
                "month": month,
                "month_name": MONTH_NAMES[month - 1],
                "expected_days": expected,
                "temperatures": statistics,
                "tavg_taxn_comparison": {
                    "paired_days": len(differences),
                    "mean_difference": _mean(differences),
                    "mean_absolute_difference": _mean([abs(value) for value in differences]),
                    "maximum_absolute_difference": round(max(map(abs, differences)), 2) if differences else None,
                },
            }
        )

    return {
        **base,
        "export_type": "extended",
        "station_id": station_id,
        "overall_completeness": _completeness(rows),
        "monthly_statistics": monthly_statistics,
    }


def fetch_temperature_export(
    station_id: str,
    start_year: int,
    end_year: int,
    *,
    mode: str,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
    timeout: float = 30,
    concurrency: int = 4,
    max_attempts: int = 2,
    cache_dir: str | Path | None = None,
    cache_ttl_seconds: int = 30 * 24 * 60 * 60,
    refresh: bool = False,
) -> dict[str, Any]:
    """Build daily, monthly or extended temperature exports with explicit methods."""

    normalized_mode = mode.strip().lower()
    if normalized_mode not in TEMPERATURE_EXPORT_MODES:
        raise ValueError(f"mode must be one of: {', '.join(TEMPERATURE_EXPORT_MODES)}")
    normalized_station_id = _normalize_ghcnd_station_id(station_id)
    _validate_temperature_year_range(start_year, end_year)
    requested_years = list(range(start_year, end_year + 1))
    year_records, missing_report, token_usage, adaptive_history = _fetch_temperature_record_range(
        normalized_station_id,
        start_year,
        end_year,
        CORE_TEMPERATURE_DATATYPES,
        token=token,
        token_provider=token_provider,
        timeout=timeout,
        concurrency=concurrency,
        max_attempts=max_attempts,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
        refresh=refresh,
    )

    today_parts = time.gmtime()
    today = date(today_parts.tm_year, today_parts.tm_mon, today_parts.tm_mday)
    rows: list[dict[str, Any]] = []
    quality_control = {
        "records_received": 0,
        "records_used": 0,
        "records_rejected_quality": 0,
        "records_rejected_invalid": 0,
    }
    observed_datatypes: set[str] = set()
    for year in requested_years:
        final_day = min(date(year, 12, 31), today)
        year_rows, year_quality = _daily_temperature_rows(year, year_records[year], final_day=final_day)
        rows.extend(year_rows)
        for key in quality_control:
            quality_control[key] += year_quality[key]
        for row in year_rows:
            for datatype, field in (("TMIN", "tmin"), ("TAVG", "tavg"), ("TMAX", "tmax")):
                if isinstance(row.get(field), (int, float)):
                    observed_datatypes.add(datatype)
        if not any(
            isinstance(row.get(field), (int, float))
            for row in year_rows
            for field in ("tmin", "tavg", "tmax")
        ):
            missing_report.setdefault(year, "No usable temperature data after quality control")

    base = {
        "schema_version": 1,
        "dataset_id": "GHCND",
        "units": "celsius",
        "period": {
            "start_date": date(start_year, 1, 1).isoformat(),
            "end_date": min(date(end_year, 12, 31), today).isoformat(),
        },
        "requested_datatypes": list(CORE_TEMPERATURE_DATATYPES),
        "observed_datatypes": [
            datatype for datatype in CORE_TEMPERATURE_DATATYPES if datatype in observed_datatypes
        ],
        "derived_datatypes": ["TAXN", "AMPLITUDE"],
        "temperature_methods": _temperature_method_metadata(),
        "aggregation_methods": {
            "monthly_mean": "arithmetic_mean_of_available_quality_controlled_daily_values",
            "completeness_percent": "valid_observed_days / expected_calendar_days * 100",
            "quality_rule": "records_with_a_non_empty_NOAA_QFLAG_are_excluded",
        },
        "quality_control": quality_control,
        "missing_data_report": {str(year): missing_report[year] for year in sorted(missing_report)},
        "token_usage": token_usage,
        "adaptive_history": adaptive_history,
    }
    if normalized_mode == "daily":
        return {
            **base,
            "export_type": "daily",
            "station_id": normalized_station_id,
            "completeness": _completeness(rows),
            "data": rows,
        }
    if normalized_mode == "monthly":
        return _monthly_temperature_export(normalized_station_id, requested_years, rows, base)
    return _extended_temperature_export(normalized_station_id, rows, base)


def fetch_stations_with_cache_details(
    cache_path: str | Path | None = None,
    remote_url: str | None = None,
    cache_ttl: int = 3600,
    refresh: bool = False,
    allow_sample_fallback: bool = False,
    stale_if_error: bool = False,
    max_stale_seconds: int | None = None,
    token: str | None = None,
    token_provider: TokenProvider | None = None,
    remote_timeout_seconds: float = 10,
) -> FetchResult:
    cache_file: Path | None = Path(cache_path) if cache_path is not None else None
    remote = remote_url or os.getenv("NOAA_STATIONS_URL")
    cache_metadata = read_cache_metadata(cache_file) if cache_file is not None else {}
    remote_host = urlparse(remote).hostname if remote else None
    log_event(
        "station_fetch_started",
        remote_host=remote_host,
        refresh=refresh,
        cache_enabled=cache_file is not None,
    )

    if cache_file is not None and cache_file.exists() and not refresh:
        effective_ttl = max(cache_ttl, 0)
        age_seconds = _resolve_cache_age_seconds(cache_file, cache_metadata)
        source_match = _cache_matches_remote_source(cache_metadata, remote)
        if age_seconds <= effective_ttl and source_match:
            cached = load_stations(cache_file)
            if cached:
                log_event(
                    "station_fetch_completed",
                    source="cache-fresh",
                    count=len(cached),
                    cache_age_seconds=age_seconds,
                )
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
        provider = shared_env_token_provider()
    if remote:
        try:
            fetched, remote_meta = fetch_remote_stations(
                remote,
                timeout=remote_timeout_seconds,
                token=remote_token,
                token_provider=provider,
            )
        except NoaaClientError as exc:
            log_event(
                "station_fetch_failed",
                level=logging.WARNING,
                remote_host=remote_host,
                error_type=type(exc).__name__,
            )
            if stale_if_error and cache_file is not None and cache_file.exists():
                stale_cached = load_stations(cache_file)
                stale_age = _resolve_cache_age_seconds(cache_file, cache_metadata)
                stale_allowed = max_stale_seconds is None or stale_age <= max_stale_seconds
                if stale_cached:
                    if not stale_allowed:
                        raise NoaaNetworkError(
                            f"Stale cache age {stale_age}s exceeds max_stale_seconds={max_stale_seconds}"
                        ) from exc
                    log_event(
                        "station_fetch_completed",
                        source="cache-stale",
                        count=len(stale_cached),
                        cache_age_seconds=stale_age,
                    )
                    return FetchResult(
                        stations=stale_cached,
                        source="cache-stale",
                        metadata={
                            "cache_path": str(cache_file),
                            "cache_age_seconds": stale_age,
                            "max_stale_seconds": max_stale_seconds,
                            "cache_metadata": cache_metadata,
                            "warning": str(exc),
                            "remote_url": remote,
                        },
                    )
            if allow_sample_fallback:
                log_event("station_fetch_completed", source="sample-fallback", count=len(STATIONS))
                return FetchResult(
                    stations=[_clone_station(station) for station in STATIONS],
                    source="sample-fallback",
                    metadata={"warning": str(exc), "remote_url": remote},
                )
            raise

        if fetched:
            if cache_file is not None:
                now_ts = int(time.time())
                _write_json_atomic(cache_file, fetched)
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
            log_event(
                "station_fetch_completed",
                source="remote",
                count=len(fetched),
                remote_host=remote_host,
                http_status=remote_meta.get("http_status"),
            )
            return FetchResult(
                stations=fetched,
                source="remote",
                metadata={"remote_url": remote, **remote_meta},
            )

        if allow_sample_fallback:
            log_event("station_fetch_completed", source="sample-fallback", count=len(STATIONS))
            return FetchResult(
                stations=[_clone_station(station) for station in STATIONS],
                source="sample-fallback",
                metadata={"remote_url": remote, "warning": "Remote returned no stations"},
            )

        raise NoaaPayloadError("Remote returned no usable stations")

    if cache_file is not None and cache_file.exists():
        cached = load_stations(cache_file)
        if cached:
            log_event("station_fetch_completed", source="cache", count=len(cached))
            return FetchResult(
                stations=cached,
                source="cache",
                metadata={
                    "cache_path": str(cache_file),
                    "cache_age_seconds": _resolve_cache_age_seconds(cache_file, cache_metadata),
                    "cache_metadata": cache_metadata,
                },
            )

    log_event("station_fetch_completed", source="sample-default", count=len(STATIONS))
    return FetchResult(
        stations=[_clone_station(station) for station in STATIONS],
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
            _write_json_atomic(output_json, payload)
        else:
            _write_json_atomic(output_json, payload, indent=None)

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

                fieldnames = sorted({key for row in rows for key in row})
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
