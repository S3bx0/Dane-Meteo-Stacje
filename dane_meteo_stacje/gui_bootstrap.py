from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import time
import webbrowser
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import BoundedSemaphore
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from .cli import search_stations
from .data import NoaaClientError, StationRecord, fetch_stations_with_cache_details
from .diagnostics import fetch_error_code, render_fetch_error
from .observability import bind_request_id, configure_logging, log_event, reset_request_id

MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_CONCURRENT_FETCHES = 4
FETCH_SLOT_TIMEOUT_SECONDS = 0.1
GUI_CACHE_DIR = Path.home() / ".cache" / "dane-meteo-stacje"
_FETCH_LIMITER = BoundedSemaphore(MAX_CONCURRENT_FETCHES)


class InvalidRequestBody(ValueError):
    pass


class RequestBodyTooLarge(ValueError):
    pass


def _resolve_gui_cache_path(value: object) -> Path | None:
  if value is None or not str(value).strip():
    return None

  raw_name = str(value).strip()
  path_variants = (PurePosixPath(raw_name), PureWindowsPath(raw_name))
  if any(path.is_absolute() or len(path.parts) != 1 or path.name != raw_name for path in path_variants):
    raise ValueError("cache_path must be a file name without directories")
  if PurePosixPath(raw_name).suffix.lower() != ".json":
    raise ValueError("cache_path must use the .json extension")
  return GUI_CACHE_DIR / raw_name


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


HTML_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Dane Meteo Stacje GUI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
    <style>
      body { background: linear-gradient(135deg, #edf2f7 0%, #e6fffa 100%); }
      .panel { backdrop-filter: blur(4px); background: rgba(255, 255, 255, 0.92); }
      .table-wrap { max-height: 52vh; overflow: auto; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    </style>
  </head>
  <body>
    <main class="container py-4">
      <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
        <h1 class="h3 m-0">Dane Meteo Stacje</h1>
        <span id="status-badge" class="badge text-bg-secondary">Ready</span>
      </div>

      <section class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div class="row g-3">
          <div class="col-md-4">
            <label for="query" class="form-label">Query (optional)</label>
            <input id="query" class="form-control" placeholder="city or station name" />
          </div>
          <div class="col-md-2">
            <label for="country" class="form-label">Country</label>
            <input id="country" class="form-control" list="country-options" placeholder="Poland / Polska" />
            <datalist id="country-options"></datalist>
          </div>
          <div class="col-md-3">
            <label for="station-id" class="form-label">Station ID</label>
            <input id="station-id" class="form-control mono" placeholder="PL000012120" />
          </div>
          <div class="col-md-2">
            <label for="sort" class="form-label">Sort</label>
            <select id="sort" class="form-select">
              <option value="city">city</option>
              <option value="name">name</option>
              <option value="station_id">station_id</option>
            </select>
          </div>
          <div class="col-md-1">
            <label for="limit" class="form-label">Limit</label>
            <input id="limit" type="number" min="1" class="form-control" placeholder="10" />
          </div>

          <div class="col-md-7">
            <label for="remote-url" class="form-label">Remote URL</label>
            <input
              id="remote-url"
              class="form-control mono"
              placeholder="https://www.ncei.noaa.gov/cdo-web/api/v2/stations?..."
            />
            <div class="form-text">Example: FIPS:PL (Poland), FIPS:HU (Hungary), FIPS:SP (Spain).</div>
          </div>
          <div class="col-md-3">
            <label for="cache-path" class="form-label">Cache File</label>
            <input id="cache-path" class="form-control mono" placeholder="cache.json" />
          </div>
          <div class="col-md-2">
            <label for="cache-ttl" class="form-label">Cache TTL (s)</label>
            <input id="cache-ttl" type="number" min="0" class="form-control" value="3600" />
          </div>

          <div class="col-12 d-flex flex-wrap gap-3 align-items-center">
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="refresh" />
              <label class="form-check-label" for="refresh">Refresh</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="stale-if-error" />
              <label class="form-check-label" for="stale-if-error">Stale if error</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="allow-sample" />
              <label class="form-check-label" for="allow-sample">Allow sample fallback</label>
            </div>
            <div class="d-flex align-items-center gap-2">
              <label for="max-stale" class="form-label m-0">Max stale (s)</label>
              <input id="max-stale" type="number" min="0" class="form-control form-control-sm" style="width: 8rem;" />
            </div>
          </div>

          <div class="col-12 d-flex flex-wrap gap-2">
            <button id="search-btn" class="btn btn-primary">Search</button>
            <button id="export-json-btn" class="btn btn-outline-secondary" disabled>Export JSON</button>
            <button id="export-csv-btn" class="btn btn-outline-secondary" disabled>Export CSV</button>
          </div>
        </div>
      </section>

      <section class="panel rounded-4 shadow-sm p-3 p-md-4">
        <div id="message" class="small text-secondary mb-2">No results yet.</div>
        <div class="table-wrap">
          <table class="table table-sm table-hover align-middle mb-0">
            <thead class="table-light sticky-top">
              <tr>
                <th>City</th>
                <th>Name</th>
                <th>Station ID</th>
                <th>Country</th>
                <th>Lat</th>
                <th>Lon</th>
              </tr>
            </thead>
            <tbody id="result-body"></tbody>
          </table>
        </div>
      </section>
    </main>

    <script>
      let lastResults = [];

      const COUNTRY_OPTIONS = [
        "Afghanistan",
        "Albania",
        "Algeria",
        "Andorra",
        "Angola",
        "Antigua and Barbuda",
        "Argentina",
        "Armenia",
        "Australia",
        "Austria",
        "Azerbaijan",
        "Bahamas",
        "Bahrain",
        "Bangladesh",
        "Barbados",
        "Belarus",
        "Belgium",
        "Belize",
        "Benin",
        "Bhutan",
        "Bolivia",
        "Bosnia and Herzegovina",
        "Botswana",
        "Brazil",
        "Brunei",
        "Bulgaria",
        "Burkina Faso",
        "Burundi",
        "Cabo Verde",
        "Cambodia",
        "Cameroon",
        "Canada",
        "Central African Republic",
        "Chad",
        "Chile",
        "China",
        "Colombia",
        "Comoros",
        "Congo",
        "Costa Rica",
        "Cote d'Ivoire",
        "Croatia",
        "Cuba",
        "Cyprus",
        "Czechia",
        "Democratic Republic of the Congo",
        "Denmark",
        "Djibouti",
        "Dominica",
        "Dominican Republic",
        "Ecuador",
        "Egypt",
        "El Salvador",
        "Equatorial Guinea",
        "Eritrea",
        "Estonia",
        "Eswatini",
        "Ethiopia",
        "Fiji",
        "Finland",
        "France",
        "Gabon",
        "Gambia",
        "Georgia",
        "Germany",
        "Ghana",
        "Greece",
        "Grenada",
        "Guatemala",
        "Guinea",
        "Guinea-Bissau",
        "Guyana",
        "Haiti",
        "Honduras",
        "Hungary",
        "Iceland",
        "India",
        "Indonesia",
        "Iran",
        "Iraq",
        "Ireland",
        "Israel",
        "Italy",
        "Jamaica",
        "Japan",
        "Jordan",
        "Kazakhstan",
        "Kenya",
        "Kiribati",
        "Kuwait",
        "Kyrgyzstan",
        "Laos",
        "Latvia",
        "Lebanon",
        "Lesotho",
        "Liberia",
        "Libya",
        "Liechtenstein",
        "Lithuania",
        "Luxembourg",
        "Madagascar",
        "Malawi",
        "Malaysia",
        "Maldives",
        "Mali",
        "Malta",
        "Marshall Islands",
        "Mauritania",
        "Mauritius",
        "Mexico",
        "Micronesia",
        "Moldova",
        "Monaco",
        "Mongolia",
        "Montenegro",
        "Morocco",
        "Mozambique",
        "Myanmar",
        "Namibia",
        "Nauru",
        "Nepal",
        "Netherlands",
        "New Zealand",
        "Nicaragua",
        "Niger",
        "Nigeria",
        "North Korea",
        "North Macedonia",
        "Norway",
        "Oman",
        "Pakistan",
        "Palau",
        "Palestine",
        "Panama",
        "Papua New Guinea",
        "Paraguay",
        "Peru",
        "Philippines",
        "Poland",
        "Portugal",
        "Qatar",
        "Romania",
        "Russia",
        "Rwanda",
        "Saint Kitts and Nevis",
        "Saint Lucia",
        "Saint Vincent and the Grenadines",
        "Samoa",
        "San Marino",
        "Sao Tome and Principe",
        "Saudi Arabia",
        "Senegal",
        "Serbia",
        "Seychelles",
        "Sierra Leone",
        "Singapore",
        "Slovakia",
        "Slovenia",
        "Solomon Islands",
        "Somalia",
        "South Africa",
        "South Korea",
        "South Sudan",
        "Spain",
        "Sri Lanka",
        "Sudan",
        "Suriname",
        "Sweden",
        "Switzerland",
        "Syria",
        "Tajikistan",
        "Tanzania",
        "Thailand",
        "Timor-Leste",
        "Togo",
        "Tonga",
        "Trinidad and Tobago",
        "Tunisia",
        "Turkey",
        "Turkmenistan",
        "Tuvalu",
        "Uganda",
        "Ukraine",
        "United Arab Emirates",
        "United Kingdom",
        "USA",
        "Uruguay",
        "Uzbekistan",
        "Vanuatu",
        "Vatican City",
        "Venezuela",
        "Vietnam",
        "Yemen",
        "Zambia",
        "Zimbabwe",
      ].sort((a, b) => a.localeCompare(b));

      const COUNTRY_ALIASES = {
        "at": "Austria",
        "aus": "Austria",
        "austria": "Austria",
        "hu": "Hungary",
        "hun": "Hungary",
        "hungary": "Hungary",
        "węgry": "Hungary",
        "weg": "Hungary",
        "wegry": "Hungary",
        "pl": "Poland",
        "pol": "Poland",
        "poland": "Poland",
        "polska": "Poland",
        "de": "Germany",
        "ger": "Germany",
        "germany": "Germany",
        "niemcy": "Germany",
        "cz": "Czechia",
        "czech": "Czechia",
        "czechia": "Czechia",
        "czech republic": "Czechia",
        "us": "USA",
        "usa": "USA",
        "uk": "United Kingdom",
        "gb": "United Kingdom",
        "united kingdom": "United Kingdom",
        "wielka brytania": "United Kingdom",
      };

      function byId(id) { return document.getElementById(id); }
      function maybeInt(v) {
        if (v === null || v === undefined || v === "") return null;
        const parsed = Number(v);
        return Number.isFinite(parsed) ? parsed : null;
      }

      function normalizeCountryInput(rawValue) {
        const trimmed = (rawValue || "").trim();
        if (!trimmed) return null;

        const key = trimmed.toLowerCase();
        if (COUNTRY_ALIASES[key]) return COUNTRY_ALIASES[key];

        const matched = COUNTRY_OPTIONS.find((item) => item.toLowerCase() === key);
        return matched || trimmed;
      }

      function initCountryAutocomplete() {
        const datalist = byId("country-options");
        datalist.innerHTML = "";
        for (const country of COUNTRY_OPTIONS) {
          const option = document.createElement("option");
          option.value = country;
          datalist.appendChild(option);
        }
      }

      function setStatus(text, variant) {
        const badge = byId("status-badge");
        badge.className = `badge text-bg-${variant}`;
        badge.textContent = text;
      }

      function renderResults(rows) {
        const body = byId("result-body");
        body.replaceChildren();
        for (const row of rows) {
          const tr = document.createElement("tr");
          for (const [value, className] of [
            [row.city, ""],
            [row.name, ""],
            [row.station_id, "mono"],
            [row.country, ""],
            [row.latitude, ""],
            [row.longitude, ""],
          ]) {
            const cell = document.createElement("td");
            cell.textContent = value ?? "";
            if (className) cell.className = className;
            tr.appendChild(cell);
          }
          body.appendChild(tr);
        }
      }

      async function runSearch() {
        setStatus("Loading", "warning");
        byId("message").textContent = "Searching...";

        const payload = {
          query: byId("query").value,
          country: normalizeCountryInput(byId("country").value),
          station_id: byId("station-id").value || null,
          sort: byId("sort").value,
          limit: maybeInt(byId("limit").value),
          remote_url: byId("remote-url").value || null,
          cache_path: byId("cache-path").value || null,
          cache_ttl: maybeInt(byId("cache-ttl").value) ?? 3600,
          refresh: byId("refresh").checked,
          stale_if_error: byId("stale-if-error").checked,
          allow_sample_fallback: byId("allow-sample").checked,
          max_stale: maybeInt(byId("max-stale").value),
        };

        try {
          const resp = await fetch("/api/search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await resp.json();
          if (!resp.ok) {
            setStatus("Error", "danger");
            byId("message").textContent = `${data.code ?? "ERROR"}: ${data.message ?? "Unknown error"}`;
            return;
          }

          lastResults = data.results || [];
          renderResults(lastResults);
          byId("export-json-btn").disabled = lastResults.length === 0;
          byId("export-csv-btn").disabled = lastResults.length === 0;

          const source = data.source || "unknown";
          let msg = `Found ${lastResults.length} station(s). Source: ${source}.`;
          const meta = data.metadata || {};
          if (meta.warning) {
            msg += ` Warning: ${meta.warning}`;
          }
          if (meta.normalization && meta.normalization.items_invalid > 0) {
            msg += ` Dropped NOAA rows: ${meta.normalization.items_invalid}/${meta.normalization.items_total}.`;
          }
          if (lastResults.length === 0) {
            if (payload.station_id) {
              msg += " Hint: clear Station ID to widen search.";
            }
            if (!payload.remote_url && source === "sample-default") {
              msg += " Hint: add NOAA Remote URL; local sample has only demo stations.";
            }
          }
          byId("message").textContent = msg;
          setStatus("OK", "success");
        } catch (err) {
          setStatus("Error", "danger");
          byId("message").textContent = `Request failed: ${err}`;
        }
      }

      async function exportResults(fmt) {
        if (!lastResults.length) return;
        const resp = await fetch("/api/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ format: fmt, rows: lastResults }),
        });
        if (!resp.ok) {
          const data = await resp.json();
          byId("message").textContent = `${data.code ?? "ERROR"}: ${data.message ?? "Export failed"}`;
          return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = fmt === "csv" ? "stations.csv" : "stations.json";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      }

      byId("search-btn").addEventListener("click", runSearch);
      byId("export-json-btn").addEventListener("click", () => exportResults("json"));
      byId("export-csv-btn").addEventListener("click", () => exportResults("csv"));
      initCountryAutocomplete();
    </script>
  </body>
</html>
"""


def _parse_int(value: Any, *, default: int | None = None, minimum: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    parsed = int(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"Value must be >= {minimum}")
    return parsed


def _parse_content_length(raw_value: str | None) -> int:
    try:
        length = int(raw_value or "0")
    except ValueError as exc:
        raise InvalidRequestBody("Invalid Content-Length header") from exc
    if length < 0:
        raise InvalidRequestBody("Content-Length must be non-negative")
    if length > MAX_REQUEST_BODY_BYTES:
        raise RequestBodyTooLarge(f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes")
    return length


def _validate_remote_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        raise ValueError("Remote URL must be a valid HTTPS URL")
    if hostname != "noaa.gov" and not hostname.endswith(".noaa.gov"):
        raise ValueError("Remote URL host must belong to noaa.gov")
    if parsed.username or parsed.password:
        raise ValueError("Remote URL must not contain credentials")
    return value


def _csv_from_rows(rows: list[StationRecord]) -> str:
    output = io.StringIO()
    fieldnames = ["station_id", "city", "name", "country", "latitude", "longitude", "source", "notes"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return output.getvalue()


class AppHandler(BaseHTTPRequestHandler):
    request_id = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._run_request("GET", self._dispatch_get)

    def do_POST(self) -> None:
        self._run_request("POST", self._dispatch_post)

    def _run_request(self, method: str, dispatch: Callable[[], None]) -> None:
        self.request_id = uuid4().hex
        context_token = bind_request_id(self.request_id)
        path = urlparse(self.path).path
        started_at = time.monotonic()
        log_event("http_request_started", method=method, path=path)
        try:
            dispatch()
        except (BrokenPipeError, ConnectionResetError):
            log_event("http_client_disconnected", level=logging.WARNING, method=method, path=path)
        except Exception as exc:
            log_event(
                "http_request_failed",
                level=logging.ERROR,
                exc_info=True,
                method=method,
                path=path,
                error_type=type(exc).__name__,
            )
            try:
                self._send_json(
                    {"code": "INTERNAL_ERROR", "message": "Internal server error"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            except (BrokenPipeError, ConnectionResetError):
                log_event("http_client_disconnected", level=logging.WARNING, method=method, path=path)
        finally:
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            log_event("http_request_completed", method=method, path=path, duration_ms=duration_ms)
            reset_request_id(context_token)

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(HTML_PAGE, content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            self._send_json({"ok": True})
            return
        self._send_json({"code": "NOT_FOUND", "message": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _dispatch_post(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/search":
                self._handle_search()
                return
            if parsed.path == "/api/export":
                self._handle_export()
                return
            self._send_json({"code": "NOT_FOUND", "message": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except RequestBodyTooLarge as exc:
            self._send_json(
                {"code": "PAYLOAD_TOO_LARGE", "message": str(exc)},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except InvalidRequestBody as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _read_json(self) -> dict[str, Any]:
        length = _parse_content_length(self.headers.get("Content-Length"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            raise InvalidRequestBody("JSON body must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidRequestBody("Request body must be valid UTF-8 JSON") from exc

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        response_payload = {**payload, "request_id": self.request_id}
        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self.end_headers()
        self.wfile.write(body)
        log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=int(status))

    def _send_text(self, payload: str, *, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self.end_headers()
        self.wfile.write(body)
        log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=int(status))

    def _handle_search(self) -> None:
        payload = self._read_json()
        query = str(payload.get("query", "")).strip()

        try:
            cache_ttl = _parse_int(payload.get("cache_ttl"), default=3600, minimum=0)
            if cache_ttl is None:
                cache_ttl = 3600
            max_stale = _parse_int(payload.get("max_stale"), default=None, minimum=0)
            limit = _parse_int(payload.get("limit"), default=None, minimum=1)
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        remote_url_value = payload.get("remote_url")
        try:
            remote_url = _validate_remote_url(str(remote_url_value)) if remote_url_value else None
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            cache_path = _resolve_gui_cache_path(payload.get("cache_path"))
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        country = payload.get("country")
        station_id = payload.get("station_id")
        sort_by = str(payload.get("sort", "city"))

        if not _FETCH_LIMITER.acquire(timeout=FETCH_SLOT_TIMEOUT_SECONDS):
            log_event(
                "station_fetch_rejected",
                level=logging.WARNING,
                reason="concurrency_limit",
                limit=MAX_CONCURRENT_FETCHES,
            )
            self._send_json(
                {"code": "SERVER_BUSY", "message": "Server is busy; retry shortly"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        try:
            try:
                if cache_path is not None:
                    GUI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                result = fetch_stations_with_cache_details(
                    cache_path=cache_path,
                    remote_url=str(remote_url) if remote_url else None,
                    cache_ttl=cache_ttl,
                    refresh=bool(payload.get("refresh", False)),
                    allow_sample_fallback=bool(payload.get("allow_sample_fallback", False)),
                    stale_if_error=bool(payload.get("stale_if_error", False)),
                    max_stale_seconds=max_stale,
                )
            finally:
                _FETCH_LIMITER.release()

            normalized_country = str(country).strip() if country else None
            normalized_station_id = str(station_id).strip() if station_id else None

            if query:
                rows = search_stations(
                    query=query,
                    stations=result.stations,
                    limit=limit,
                    country=normalized_country,
                    station_id=normalized_station_id,
                    sort_by=sort_by,
                )
            else:
                rows = list(result.stations)
                if normalized_country:
                    rows = [
                        row
                        for row in rows
                        if str(row.get("country", "")).lower() == normalized_country.lower()
                    ]
                if normalized_station_id:
                    rows = [
                        row
                        for row in rows
                        if str(row.get("station_id", "")).lower() == normalized_station_id.lower()
                    ]

                if sort_by == "name":
                    rows = sorted(rows, key=lambda row: str(row.get("name", "")).lower())
                elif sort_by == "station_id":
                    rows = sorted(rows, key=lambda row: str(row.get("station_id", "")).lower())
                else:
                    rows = sorted(rows, key=lambda row: str(row.get("city", "")).lower())

                if limit is not None:
                    rows = rows[:limit]
        except NoaaClientError as exc:
            self._send_json(
                {
                    "code": fetch_error_code(exc),
                    "message": render_fetch_error(exc),
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return

        self._send_json(
            {
                "results": rows,
                "source": result.source,
                "metadata": result.metadata,
            }
        )

    def _handle_export(self) -> None:
        payload = self._read_json()
        fmt = str(payload.get("format", "json")).lower()
        raw_rows = payload.get("rows", [])

        if not isinstance(raw_rows, list):
            self._send_json(
                {"code": "BAD_REQUEST", "message": "rows must be an array"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        rows: list[StationRecord] = []
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            station_id = str(item.get("station_id", "")).strip()
            city = str(item.get("city", "")).strip()
            name = str(item.get("name", "")).strip()
            country = str(item.get("country", "")).strip()
            if not station_id or not city or not name or not country:
                continue
            row: StationRecord = {
                "station_id": station_id,
                "city": city,
                "name": name,
                "country": country,
            }
            if "latitude" in item:
                try:
                    row["latitude"] = float(item["latitude"])
                except (TypeError, ValueError):
                    pass
            if "longitude" in item:
                try:
                    row["longitude"] = float(item["longitude"])
                except (TypeError, ValueError):
                    pass
            if "source" in item:
                row["source"] = str(item["source"])
            if "notes" in item:
                row["notes"] = str(item["notes"])
            rows.append(row)

        if fmt == "csv":
            content = _csv_from_rows(rows)
            body = content.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="stations.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", self.request_id)
            self.end_headers()
            self.wfile.write(body)
            log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=HTTPStatus.OK)
            return

        if fmt == "json":
            body = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="stations.json"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", self.request_id)
            self.end_headers()
            self.wfile.write(body)
            log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=HTTPStatus.OK)
            return

        self._send_json(
            {"code": "BAD_REQUEST", "message": "format must be json or csv"},
            status=HTTPStatus.BAD_REQUEST,
        )


def run_server(host: str, port: int, *, open_browser: bool = True) -> None:
    server = AppHTTPServer((host, port), AppHandler)
    url = f"http://{host}:{port}"
    print(f"GUI is running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Bootstrap GUI for Dane Meteo Stacje")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the web server")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind the web server")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Structured log level",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open browser window",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    configure_logging(args.log_level)
    run_server(args.host, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
