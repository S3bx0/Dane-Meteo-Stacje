from __future__ import annotations

from .data import (
    NoaaAuthError,
    NoaaClientError,
    NoaaNetworkError,
    NoaaPayloadError,
    NoaaRateLimitError,
    NoaaTimeoutError,
)


def render_fetch_error(exc: NoaaClientError) -> str:
    if isinstance(exc, NoaaAuthError):
        return "NOAA auth error: sprawdź token (NOAA_TOKEN) lub uprawnienia."
    if isinstance(exc, NoaaRateLimitError):
        return "NOAA rate limit: przekroczono limit zapytań (HTTP 429)."
    if isinstance(exc, NoaaPayloadError):
        return "NOAA payload error: nieobsługiwany format odpowiedzi NOAA."
    if isinstance(exc, NoaaTimeoutError):
        return "NOAA timeout: przekroczono limit czasu pobierania danych."
    if isinstance(exc, NoaaNetworkError):
        return "NOAA network error: nie udało się pobrać danych z NOAA."
    return "NOAA error: nie udało się pobrać danych."


def fetch_error_code(exc: NoaaClientError) -> str:
    if isinstance(exc, NoaaAuthError):
        return "NOAA_AUTH"
    if isinstance(exc, NoaaRateLimitError):
        return "NOAA_RATE_LIMIT"
    if isinstance(exc, NoaaPayloadError):
        return "NOAA_PAYLOAD"
    if isinstance(exc, NoaaTimeoutError):
        return "NOAA_TIMEOUT"
    if isinstance(exc, NoaaNetworkError):
        return "NOAA_NETWORK"
    return "NOAA_UNKNOWN"
