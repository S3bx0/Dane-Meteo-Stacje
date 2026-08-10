from __future__ import annotations

from collections import Counter
from threading import Lock


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._errors: Counter[str] = Counter()
        self._fallbacks: Counter[str] = Counter()
        self._server_busy = 0
        self._duration_count = 0
        self._duration_sum = 0.0
        self._active_fetches = 0

    def record_request(self, method: str, path: str, status: int, duration_seconds: float) -> None:
        with self._lock:
            self._requests[(method, path, status)] += 1
            self._duration_count += 1
            self._duration_sum += max(duration_seconds, 0.0)
            if status >= 400:
                self._errors[str(status)] += 1

    def record_fallback(self, source: str) -> None:
        with self._lock:
            self._fallbacks[source] += 1

    def record_server_busy(self) -> None:
        with self._lock:
            self._server_busy += 1

    def fetch_started(self) -> None:
        with self._lock:
            self._active_fetches += 1

    def fetch_finished(self) -> None:
        with self._lock:
            self._active_fetches = max(self._active_fetches - 1, 0)

    def render_prometheus(self) -> str:
        with self._lock:
            requests = self._requests.copy()
            errors = self._errors.copy()
            fallbacks = self._fallbacks.copy()
            server_busy = self._server_busy
            duration_count = self._duration_count
            duration_sum = self._duration_sum
            active_fetches = self._active_fetches

        lines = [
            "# HELP dane_meteo_requests_total HTTP requests by method, route, and status.",
            "# TYPE dane_meteo_requests_total counter",
        ]
        for (method, path, status), count in sorted(requests.items()):
            labels = (
                f'method="{_escape_label(method)}",path="{_escape_label(path)}",status="{status}"'
            )
            lines.append(f"dane_meteo_requests_total{{{labels}}} {count}")
        lines.extend(
            [
                "# HELP dane_meteo_request_duration_seconds HTTP request duration.",
                "# TYPE dane_meteo_request_duration_seconds summary",
                f"dane_meteo_request_duration_seconds_count {duration_count}",
                f"dane_meteo_request_duration_seconds_sum {duration_sum:.6f}",
                "# HELP dane_meteo_errors_total HTTP errors by status.",
                "# TYPE dane_meteo_errors_total counter",
            ]
        )
        for error_status, error_count in sorted(errors.items()):
            lines.append(f'dane_meteo_errors_total{{status="{error_status}"}} {error_count}')
        lines.extend(
            [
                "# HELP dane_meteo_fallbacks_total Station data fallbacks by source.",
                "# TYPE dane_meteo_fallbacks_total counter",
            ]
        )
        for source, fallback_count in sorted(fallbacks.items()):
            lines.append(
                f'dane_meteo_fallbacks_total{{source="{_escape_label(source)}"}} {fallback_count}'
            )
        lines.extend(
            [
                "# HELP dane_meteo_server_busy_total Requests rejected by the fetch limit.",
                "# TYPE dane_meteo_server_busy_total counter",
                f"dane_meteo_server_busy_total {server_busy}",
                "# HELP dane_meteo_active_fetches Current station fetch operations.",
                "# TYPE dane_meteo_active_fetches gauge",
                f"dane_meteo_active_fetches {active_fetches}",
            ]
        )
        return "\n".join(lines) + "\n"
