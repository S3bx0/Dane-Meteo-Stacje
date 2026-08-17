from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import logging
import os
import tempfile
import time
import webbrowser
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

import requests

from .api_contract import OPENAPI_DOCUMENT
from .cli import search_stations
from .countries import COUNTRY_FIPS_CODES, country_to_fips_code
from .data import (
    NOAA_ALLOWED_HOSTS,
    NoaaClientError,
    NoaaTimeoutError,
    StationRecord,
    TokenProvider,
    fetch_monthly_temperature_matrix,
    fetch_station_temperature_capabilities,
    fetch_stations_for_country,
    fetch_stations_with_cache_details,
    fetch_temperature_export,
    normalize_country_name,
    private_env_file_path,
    resolve_env_file,
    station_quality_summary,
)
from .diagnostics import fetch_error_code, render_fetch_error
from .metrics import MetricsRegistry
from .observability import (
    bind_request_id,
    configure_logging,
    log_event,
    reset_request_id,
)

MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_CONCURRENT_FETCHES = 4
FETCH_SLOT_TIMEOUT_SECONDS = 0.1
REMOTE_REQUEST_DEADLINE_SECONDS = 15.0
COUNTRY_BOUNDARY_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
COUNTRY_BOUNDARY_MAX_BYTES = 8 * 1024 * 1024
NOMINATIM_DEFAULT_URL = "https://nominatim.openstreetmap.org"
GUI_CACHE_DIR = private_env_file_path().parent / "cache"
_FETCH_LIMITER = BoundedSemaphore(MAX_CONCURRENT_FETCHES)
_METRICS = MetricsRegistry()
_BOUNDARY_FETCH_LOCK = Lock()
_LAST_BOUNDARY_FETCH_AT = 0.0
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_STATIC_ASSETS = {
  "/static/vendor/leaflet/leaflet.css": (
    _STATIC_ROOT / "vendor" / "leaflet" / "leaflet.css",
    "text/css; charset=utf-8",
  ),
  "/static/vendor/leaflet/leaflet.js": (
    _STATIC_ROOT / "vendor" / "leaflet" / "leaflet.js",
    "text/javascript; charset=utf-8",
  ),
  "/static/vendor/leaflet-markercluster/MarkerCluster.css": (
    _STATIC_ROOT / "vendor" / "leaflet-markercluster" / "MarkerCluster.css",
    "text/css; charset=utf-8",
  ),
  "/static/vendor/leaflet-markercluster/leaflet.markercluster.js": (
    _STATIC_ROOT / "vendor" / "leaflet-markercluster" / "leaflet.markercluster.js",
    "text/javascript; charset=utf-8",
  ),
}
_KNOWN_METRIC_PATHS = {
  "/",
  "/api/export",
  "/api/country-boundary",
  "/api/search",
  "/api/temperature-capabilities",
  "/api/temperatures",
  "/health",
  "/health/live",
  "/health/ready",
  "/metrics",
  "/openapi.json",
}


class InvalidRequestBody(ValueError):
    pass


class RequestBodyTooLarge(ValueError):
    pass


class CountryBoundaryError(RuntimeError):
    pass


def _country_cache_path(country: str) -> Path:
  country_code = os.path.basename(country_to_fips_code(country))
  base_path = os.path.realpath(GUI_CACHE_DIR / "stations")
  full_path = os.path.realpath(os.path.join(base_path, f"{country_code}.json"))
  if not full_path.startswith(base_path + os.sep):
    raise ValueError("country cache path must stay inside the managed cache directory")
  return Path(full_path)


def _country_boundary_cache_path(country: str) -> Path:
  country_code = os.path.basename(country_to_fips_code(country))
  base_path = os.path.realpath(GUI_CACHE_DIR / "boundaries")
  full_path = os.path.realpath(os.path.join(base_path, f"{country_code}.geojson"))
  if not full_path.startswith(base_path + os.sep):
    raise ValueError("country boundary path must stay inside the managed cache directory")
  return Path(full_path)


def _read_country_boundary_cache(country: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
  cache_path = _country_boundary_cache_path(country)
  try:
    age_seconds = max(time.time() - cache_path.stat().st_mtime, 0.0)
    if not allow_stale and age_seconds > COUNTRY_BOUNDARY_CACHE_TTL_SECONDS:
      return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("type") != "Feature":
      return None
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
      return None
    return payload
  except (OSError, TypeError, ValueError, json.JSONDecodeError):
    return None


def _write_country_boundary_cache(country: str, feature: dict[str, Any]) -> None:
  cache_path = _country_boundary_cache_path(country)
  cache_path.parent.mkdir(parents=True, exist_ok=True)
  temporary_path: str | None = None
  try:
    with tempfile.NamedTemporaryFile(
      mode="w",
      encoding="utf-8",
      dir=cache_path.parent,
      prefix=f".{cache_path.name}.",
      suffix=".tmp",
      delete=False,
    ) as handle:
      temporary_path = handle.name
      json.dump(feature, handle, ensure_ascii=False, separators=(",", ":"))
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary_path, cache_path)
  finally:
    if temporary_path:
      try:
        Path(temporary_path).unlink(missing_ok=True)
      except OSError:
        pass


def fetch_country_boundary(country: str) -> tuple[dict[str, Any], str]:
  """Return a cached, simplified OSM country polygon for the map overlay."""

  global _LAST_BOUNDARY_FETCH_AT
  canonical_country = normalize_country_name(country)
  cached = _read_country_boundary_cache(canonical_country)
  if cached is not None:
    return cached, "cache"

  with _BOUNDARY_FETCH_LOCK:
    cached = _read_country_boundary_cache(canonical_country)
    if cached is not None:
      return cached, "cache"

    wait_seconds = 1.0 - (time.monotonic() - _LAST_BOUNDARY_FETCH_AT)
    if wait_seconds > 0:
      time.sleep(wait_seconds)
    base_url = os.getenv("DANE_METEO_NOMINATIM_URL", NOMINATIM_DEFAULT_URL).strip().rstrip("/")
    try:
      response = requests.get(
        f"{base_url}/search",
        params={
          "country": canonical_country,
          "format": "jsonv2",
          "featureType": "country",
          "polygon_geojson": "1",
          "polygon_threshold": "0.01",
          "limit": "1",
        },
        headers={
          "User-Agent": (
            "Dane-Meteo-Stacje/0.1.1 "
            "(+https://github.com/S3bx0/Dane-Meteo-Stacje; local desktop application)"
          ),
          "Accept-Language": "pl,en;q=0.8",
        },
        timeout=REMOTE_REQUEST_DEADLINE_SECONDS,
      )
      _LAST_BOUNDARY_FETCH_AT = time.monotonic()
      response.raise_for_status()
      content_length = int(response.headers.get("Content-Length", "0") or 0)
      if content_length > COUNTRY_BOUNDARY_MAX_BYTES or len(response.content) > COUNTRY_BOUNDARY_MAX_BYTES:
        raise CountryBoundaryError("Country boundary response is too large")
      payload = response.json()
      if not isinstance(payload, list) or not payload:
        raise CountryBoundaryError("Country boundary was not found")
      result = payload[0]
      geometry = result.get("geojson") if isinstance(result, dict) else None
      if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise CountryBoundaryError("Country boundary geometry is unavailable")
      feature = {
        "type": "Feature",
        "properties": {
          "country": canonical_country,
          "display_name": str(result.get("display_name", canonical_country)),
          "attribution": "© OpenStreetMap contributors",
        },
        "geometry": geometry,
      }
      _write_country_boundary_cache(canonical_country, feature)
      return feature, "nominatim"
    except (requests.RequestException, ValueError, json.JSONDecodeError, CountryBoundaryError) as exc:
      stale = _read_country_boundary_cache(canonical_country, allow_stale=True)
      if stale is not None:
        return stale, "cache-stale"
      if isinstance(exc, CountryBoundaryError):
        raise
      raise CountryBoundaryError("Country boundary service is unavailable") from exc


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


HTML_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Dane Meteo Stacje GUI</title>
    <link rel="preconnect" href="https://tile.openstreetmap.org" crossorigin />
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
    <link href="/static/vendor/leaflet/leaflet.css" rel="stylesheet" />
    <link href="/static/vendor/leaflet-markercluster/MarkerCluster.css" rel="stylesheet" />
    <style>
      body { background: linear-gradient(135deg, #edf2f7 0%, #e6fffa 100%); }
      .panel { backdrop-filter: blur(4px); background: rgba(255, 255, 255, 0.92); }
      .table-wrap { max-height: 52vh; overflow: auto; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      .quality-toolbar { background: #f8fafc; border: 1px solid #e2e8f0; }
      .quality-badge { min-width: 5.6rem; }
      .quality-badge.good { color: #0f5132; background: #d1e7dd; }
      .quality-badge.medium { color: #7a3e00; background: #ffddb3; }
      .quality-badge.weak { color: #842029; background: #f8d7da; }
      .quality-badge.verified::after { content: " ✓"; }
      .station-row-good { box-shadow: inset 4px 0 #198754; }
      .station-row-medium { box-shadow: inset 4px 0 #fd7e14; }
      .station-row-weak { box-shadow: inset 4px 0 #dc3545; }
      .station-map-frame {
        position: relative;
        height: clamp(28rem, 58vh, 44rem);
        min-height: 26rem;
        overflow: hidden;
        border: 1px solid #cbd5e1;
        border-radius: 1rem;
        background: #eef2f7;
      }
      #station-map { width: 100%; height: 100%; background: #eef2f7; }
      #station-map .leaflet-control-attribution { font-size: 0.72rem; }
      #station-map .leaflet-popup-content { margin: 0.85rem 1rem; min-width: 13rem; }
      .station-map-dot.leaflet-div-icon, .station-map-cluster.leaflet-div-icon {
        border: 0;
        background: transparent;
      }
      .station-map-dot span {
        display: block;
        width: 16px;
        height: 16px;
        border: 2px solid #ffffff;
        border-radius: 50%;
        background: #64748b;
        box-shadow: 0 1px 5px rgba(15, 23, 42, 0.55);
      }
      .station-map-dot.quality-good span { background: #198754; }
      .station-map-dot.quality-medium span { background: #fd7e14; }
      .station-map-dot.quality-weak span { background: #dc3545; }
      .station-map-dot.comparison span {
        box-shadow: 0 0 0 3px rgba(13, 202, 240, 0.72), 0 1px 5px rgba(15, 23, 42, 0.55);
      }
      .station-map-dot:hover span, .station-map-dot:focus span { filter: brightness(0.82); }
      .station-map-dot.selected span {
        width: 20px;
        height: 20px;
        margin: -2px;
        background: #6f42c1;
        box-shadow: 0 0 0 3px rgba(111, 66, 193, 0.28), 0 2px 7px rgba(15, 23, 42, 0.6);
      }
      .station-map-cluster span {
        display: grid;
        width: 38px;
        height: 38px;
        place-items: center;
        border: 3px solid rgba(255, 255, 255, 0.9);
        border-radius: 50%;
        background: rgba(100, 116, 139, 0.9);
        color: #ffffff;
        font-weight: 700;
        box-shadow: 0 2px 8px rgba(15, 23, 42, 0.42);
      }
      .station-map-cluster.size-medium span { width: 44px; height: 44px; }
      .station-map-cluster.size-large span { width: 50px; height: 50px; }
      .station-map-cluster.quality-good span { background: rgba(25, 135, 84, 0.92); }
      .station-map-cluster.quality-medium span { background: rgba(253, 126, 20, 0.94); }
      .station-map-cluster.quality-weak span { background: rgba(220, 53, 69, 0.92); }
      .station-popup-title { font-weight: 700; margin-bottom: 0.25rem; }
      .station-popup-meta { color: #64748b; font-size: 0.82rem; margin-bottom: 0.6rem; }
      .map-loading-status {
        position: absolute;
        z-index: 500;
        top: 0.75rem;
        left: 50%;
        transform: translateX(-50%);
        padding: 0.35rem 0.7rem;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.94);
        box-shadow: 0 1px 5px rgba(15, 23, 42, 0.24);
        color: #475569;
        font-size: 0.78rem;
        pointer-events: none;
      }
      .map-loading-status.d-none { display: none; }
      .map-legend-dot {
        display: inline-block;
        width: 0.75rem;
        height: 0.75rem;
        border: 2px solid #ffffff;
        border-radius: 50%;
        background: #0d6efd;
        box-shadow: 0 0 0 1px #94a3b8;
      }
      .map-legend-dot.good { background: #198754; }
      .map-legend-dot.medium { background: #fd7e14; }
      .map-legend-dot.weak { background: #dc3545; }
      .map-legend-dot.selected { background: #6f42c1; }
      .map-legend-dot.comparison {
        background: #ffffff;
        box-shadow: 0 0 0 2px #0dcaf0;
      }
      .map-legend-area {
        display: inline-block;
        width: 1rem;
        height: 0.75rem;
        border: 1px solid rgba(13, 110, 253, 0.65);
        border-radius: 0.15rem;
        background: rgba(13, 110, 253, 0.12);
      }
      .preview-stat {
        height: 100%;
        padding: 0.85rem;
        border: 1px solid #e2e8f0;
        border-radius: 0.85rem;
        background: #f8fafc;
      }
      .preview-stat-value { font-size: 1.35rem; font-weight: 700; color: #0f172a; }
      .preview-chart-frame {
        min-height: 20rem;
        overflow-x: auto;
        border: 1px solid #e2e8f0;
        border-radius: 0.85rem;
        background: #ffffff;
      }
      .preview-chart-frame.compact { min-height: 12rem; }
      .preview-chart { display: block; width: 100%; min-width: 46rem; height: auto; }
      .chart-grid { stroke: #e2e8f0; stroke-width: 1; }
      .chart-axis-label { fill: #64748b; font-size: 11px; }
      .chart-line { fill: none; stroke-width: 2.5; stroke-linejoin: round; stroke-linecap: round; }
      .chart-point { stroke: #ffffff; stroke-width: 1.5; }
      .chart-bar { fill: #20c997; opacity: 0.8; }
      .series-tmin { stroke: #0d6efd; fill: #0d6efd; }
      .series-tavg { stroke: #fd7e14; fill: #fd7e14; }
      .series-tmax { stroke: #dc3545; fill: #dc3545; }
      .chart-line.series-tmin,
      .chart-line.series-tavg,
      .chart-line.series-tmax { fill: none; }
      .chart-legend-line { display: inline-block; width: 1.4rem; height: 0.22rem; border-radius: 1rem; }
      .nearby-table-wrap { max-height: 18rem; overflow: auto; }
      .comparison-selection {
        min-height: 3rem;
        padding: 0.65rem;
        border: 1px dashed #94a3b8;
        border-radius: 0.85rem;
        background: #f8fafc;
      }
      .comparison-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.35rem 0.55rem;
        border: 1px solid #bae6fd;
        border-radius: 999px;
        background: #ecfeff;
        color: #164e63;
      }
      .comparison-chip button {
        width: 1.25rem;
        height: 1.25rem;
        padding: 0;
        border: 0;
        border-radius: 50%;
        background: transparent;
        color: inherit;
        line-height: 1;
      }
      .comparison-chip button:hover { background: rgba(14, 116, 144, 0.15); }
      .comparison-table-wrap { max-height: 24rem; overflow: auto; }
      .comparison-color-key {
        display: inline-block;
        width: 0.85rem;
        height: 0.85rem;
        border-radius: 50%;
      }
      .comparison-base-badge { color: #3730a3; background: #e0e7ff; }
      @media (max-width: 767.98px) {
        .station-map-frame { height: 24rem; min-height: 24rem; }
        .preview-chart-frame { min-height: 16rem; }
      }
    </style>
  </head>
  <body>
    <main class="container py-4">
      <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
        <h1 class="h3 m-0">Dane Meteo Stacje</h1>
        <span id="status-badge" class="badge text-bg-secondary">Ready</span>
      </div>

      <div class="alert {{TOKEN_ALERT_CLASS}} py-2 small" role="status">
        <strong>Local NOAA tokens:</strong> {{TOKEN_STATUS_TEXT}}
      </div>

      <section class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div class="row g-3">
          <div class="col-md-4">
            <label for="query" class="form-label">Name or city filter (optional)</label>
            <input id="query" class="form-control" placeholder="e.g. Madrid; empty = every station" />
          </div>
          <div class="col-md-2">
            <label for="country" class="form-label">Country (NOAA)</label>
            <input id="country" class="form-control" list="country-options" placeholder="Poland / Polska" />
            <datalist id="country-options"></datalist>
          </div>
          <div class="col-md-3">
            <label for="station-id" class="form-label">Station ID filter (optional)</label>
            <input id="station-id" class="form-control mono" placeholder="leave empty for every station" />
          </div>
          <div class="col-md-2">
            <label for="sort" class="form-label">Sort only (not a filter)</label>
            <select id="sort" class="form-select">
              <option value="city">city</option>
              <option value="name">name</option>
              <option value="station_id">station_id</option>
            </select>
          </div>
          <div class="col-md-1">
            <label for="limit" class="form-label">Limit</label>
            <input id="limit" type="number" min="1" class="form-control" placeholder="all" />
          </div>

          <div class="col-md-10">
            <div class="form-text pt-md-4">
              Wybierz kraj — aplikacja bezpiecznie zbuduje i stronicuje zapytanie do katalogu NOAA GHCND.
              Pamięć podręczna jest zarządzana automatycznie w prywatnym katalogu użytkownika.
            </div>
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

      <section class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div id="message" class="small text-secondary mb-2">No results yet.</div>
        <div class="quality-toolbar rounded-3 p-3 mb-3">
          <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
            <div>
              <div class="fw-semibold">Ocena jakości stacji</div>
              <div class="small text-secondary">
                Ocena wstępna: kompletność katalogowa, długość okresu i aktualność danych.
                Typy TMIN/TAVG/TMAX są potwierdzane dla najlepszych kandydatów i wybranej stacji.
              </div>
            </div>
            <button id="best-station-btn" type="button" class="btn btn-success" disabled>
              Wybierz najlepszą stację
            </button>
          </div>
          <div class="row g-2 align-items-end">
            <div class="col-sm-3 col-lg-2">
              <label for="quality-min-years" class="form-label small mb-1">Minimum lat danych</label>
              <input
                id="quality-min-years" type="number" min="0" max="300" value="0"
                class="form-control form-control-sm"
              />
            </div>
            <div class="col-sm-3 col-lg-2">
              <label for="quality-min-coverage" class="form-label small mb-1">Minimum kompletności (%)</label>
              <input
                id="quality-min-coverage" type="number" min="0" max="100" value="0"
                class="form-control form-control-sm"
              />
            </div>
            <div class="col-sm-3 col-lg-2">
              <label for="quality-grade-filter" class="form-label small mb-1">Ocena</label>
              <select id="quality-grade-filter" class="form-select form-select-sm">
                <option value="all">Wszystkie</option>
                <option value="good">Dobre</option>
                <option value="medium">Średnie</option>
                <option value="weak">Słabe</option>
              </select>
            </div>
            <div class="col-sm-6 col-lg-4 d-flex flex-wrap gap-2">
              <button id="quality-preset-btn" type="button" class="btn btn-sm btn-outline-primary">
                Minimum 30 lat / 90%
              </button>
              <button id="quality-reset-btn" type="button" class="btn btn-sm btn-outline-secondary">
                Wyczyść filtry
              </button>
            </div>
          </div>
          <div id="quality-filter-summary" class="small text-secondary mt-2" aria-live="polite">
            Wyszukaj stacje, aby zastosować filtry jakości.
          </div>
        </div>
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
                <th>From</th>
                <th>To</th>
                <th>Coverage</th>
                <th>Lata</th>
                <th>Jakość</th>
                <th></th>
              </tr>
            </thead>
            <tbody id="result-body"></tbody>
          </table>
        </div>
        <div id="result-pagination" class="d-none mt-2 align-items-center gap-2">
          <span id="result-pagination-text" class="small text-secondary"></span>
          <button id="show-more-btn" type="button" class="btn btn-sm btn-outline-secondary">Show more</button>
        </div>
      </section>

      <section id="station-map-panel" class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
          <div>
            <h2 class="h5 mb-1">Interaktywna mapa stacji</h2>
            <div class="small text-secondary">
              Prawdziwy podkład mapowy, grupowanie punktów i wybór stacji bezpośrednio z mapy.
            </div>
          </div>
          <div class="btn-group btn-group-sm" role="group" aria-label="Sterowanie mapą">
            <button id="map-fit-btn" type="button" class="btn btn-outline-secondary" disabled>Dopasuj stacje</button>
            <button id="map-reset-btn" type="button" class="btn btn-outline-secondary">Pokaż świat</button>
          </div>
        </div>
        <div class="station-map-frame">
          <div
            id="station-map"
            role="application"
            aria-label="Interaktywna mapa świata ze stacjami NOAA"
          ></div>
          <div id="map-loading-status" class="map-loading-status d-none">Ładowanie podkładu mapy…</div>
        </div>
        <div class="d-flex flex-wrap justify-content-between gap-2 mt-2 small">
          <div id="map-summary" class="text-secondary">Wyszukaj stacje, aby umieścić je na mapie.</div>
          <div class="d-flex flex-wrap gap-3 text-secondary" aria-label="Legenda mapy">
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot good"></span> Dobra <strong id="quality-good-count">0</strong>
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot medium"></span> Średnia <strong id="quality-medium-count">0</strong>
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot weak"></span> Słaba <strong id="quality-weak-count">0</strong>
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot selected"></span> Wybrana stacja
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot comparison"></span> W porównaniu
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-area"></span> Wybrany kraj
            </span>
          </div>
        </div>
        <div id="map-selected-station" class="small fw-semibold mt-1" aria-live="polite">
          Nie wybrano stacji.
        </div>
        <div id="map-country-boundary" class="small text-primary mt-1" aria-live="polite"></div>
        <div class="small text-secondary mt-1">
          Podkład © OpenStreetMap contributors. Do wyświetlania szczegółów mapy wymagane jest połączenie z internetem.
        </div>
      </section>

      <section id="temperature-preview-panel" class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div class="d-flex flex-wrap justify-content-between align-items-end gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">Podgląd danych przed pobraniem</h2>
            <div id="preview-status" class="small text-secondary" aria-live="polite">
              Wybierz stację, aby zobaczyć temperatury i kompletność danych.
            </div>
          </div>
          <div class="d-flex align-items-end gap-2">
            <div>
              <label for="preview-years" class="form-label small mb-1">Okres podglądu</label>
              <select id="preview-years" class="form-select form-select-sm">
                <option value="1">1 rok</option>
                <option value="3" selected>3 lata</option>
                <option value="5">5 lat</option>
                <option value="10">10 lat</option>
              </select>
            </div>
            <button id="preview-refresh-btn" type="button" class="btn btn-sm btn-outline-primary" disabled>
              Odśwież podgląd
            </button>
          </div>
        </div>

        <div id="preview-empty" class="text-center text-secondary py-5">
          Po wybraniu stacji aplikacja pobierze niewielki miesięczny podgląd — bez tworzenia pliku JSON.
        </div>
        <div id="preview-content" class="d-none">
          <div class="row g-2 mb-3">
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Ocena jakości</div>
                <div id="preview-quality" class="preview-stat-value">—</div>
                <div id="preview-quality-detail" class="small text-secondary"></div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Dostępne typy</div>
                <div id="preview-datatypes" class="preview-stat-value">—</div>
                <div class="small text-secondary">Potwierdzone przez NOAA</div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Brakujące dni</div>
                <div id="preview-missing-days" class="preview-stat-value">—</div>
                <div id="preview-missing-detail" class="small text-secondary"></div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Niepełne lata (&lt;90%)</div>
                <div id="preview-incomplete-years" class="preview-stat-value">—</div>
                <div id="preview-incomplete-detail" class="small text-secondary"></div>
              </div>
            </div>
          </div>

          <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
            <h3 class="h6 mb-0">Miesięczne TMIN / TAVG / TMAX</h3>
            <div class="d-flex flex-wrap gap-3 small text-secondary" aria-label="Legenda wykresu temperatur">
              <span><span class="chart-legend-line bg-primary"></span> TMIN</span>
              <span><span class="chart-legend-line" style="background:#fd7e14"></span> TAVG NOAA</span>
              <span><span class="chart-legend-line bg-danger"></span> TMAX</span>
            </div>
          </div>
          <div class="preview-chart-frame mb-3">
            <svg
              id="temperature-preview-chart" class="preview-chart" viewBox="0 0 900 320"
              role="img" aria-label="Wykres miesięcznych temperatur TMIN, TAVG i TMAX"
            ></svg>
          </div>

          <h3 class="h6 mb-2">Średnia miesięczna amplituda TMAX − TMIN</h3>
          <div class="preview-chart-frame compact mb-3">
            <svg
              id="amplitude-preview-chart" class="preview-chart" viewBox="0 0 900 180"
              role="img" aria-label="Wykres miesięcznej amplitudy temperatury"
            ></svg>
          </div>

          <h3 class="h6 mb-2">Porównanie jakości najbliższych stacji</h3>
          <div class="nearby-table-wrap">
            <table class="table table-sm align-middle mb-0">
              <thead class="table-light sticky-top">
                <tr>
                  <th>Stacja</th>
                  <th>Odległość</th>
                  <th>Kompletność</th>
                  <th>Lata</th>
                  <th>Typy</th>
                  <th>Jakość</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="nearby-quality-body"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="station-comparison-panel" class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-3">
          <div>
            <h2 class="h5 mb-1">
              Porównanie stacji
              <span id="comparison-count" class="badge text-bg-info">0/5</span>
            </h2>
            <div id="comparison-status" class="small text-secondary" aria-live="polite">
              Dodaj od 2 do 5 stacji przyciskiem „Porównaj”.
            </div>
          </div>
          <div class="d-flex flex-wrap align-items-end gap-2">
            <div>
              <label for="comparison-years" class="form-label small mb-1">Okres porównania</label>
              <select id="comparison-years" class="form-select form-select-sm">
                <option value="1">1 rok</option>
                <option value="3">3 lata</option>
                <option value="5" selected>5 lat</option>
                <option value="10">10 lat</option>
              </select>
            </div>
            <div>
              <label for="comparison-datatype" class="form-label small mb-1">Parametr wykresu</label>
              <select id="comparison-datatype" class="form-select form-select-sm">
                <option value="TMIN">TMIN</option>
                <option value="TAVG" selected>TAVG NOAA</option>
                <option value="TMAX">TMAX</option>
              </select>
            </div>
            <button id="comparison-refresh-btn" type="button" class="btn btn-sm btn-primary" disabled>
              Przelicz porównanie
            </button>
            <button id="comparison-clear-btn" type="button" class="btn btn-sm btn-outline-secondary" disabled>
              Wyczyść
            </button>
          </div>
        </div>

        <div id="comparison-selection" class="comparison-selection d-flex flex-wrap gap-2 align-items-center mb-3">
          <span class="small text-secondary">Nie wybrano jeszcze żadnej stacji.</span>
        </div>

        <div id="comparison-empty" class="text-center text-secondary py-4">
          Wybierz co najmniej dwie stacje. Pierwsza dodana stacja będzie bazą dla różnic temperatur i odległości.
        </div>

        <div id="comparison-results" class="d-none">
          <div class="row g-2 mb-3">
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Wspólny zakres</div>
                <div id="comparison-common-range" class="preview-stat-value">—</div>
                <div id="comparison-active-range" class="small text-secondary"></div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Najlepsza w porównaniu</div>
                <div id="comparison-best-station" class="preview-stat-value">—</div>
                <div id="comparison-best-detail" class="small text-secondary"></div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Rozpiętość średniej</div>
                <div id="comparison-temperature-spread" class="preview-stat-value">—</div>
                <div class="small text-secondary">Maksimum − minimum dla wybranego parametru</div>
              </div>
            </div>
            <div class="col-6 col-lg-3">
              <div class="preview-stat">
                <div class="small text-secondary">Najmniej braków</div>
                <div id="comparison-lowest-missing" class="preview-stat-value">—</div>
                <div id="comparison-lowest-missing-detail" class="small text-secondary"></div>
              </div>
            </div>
          </div>

          <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
            <h3 id="comparison-chart-title" class="h6 mb-0">Porównanie miesięcznego TAVG NOAA</h3>
            <div id="comparison-chart-legend" class="d-flex flex-wrap gap-3 small text-secondary"></div>
          </div>
          <div class="preview-chart-frame mb-3">
            <svg
              id="comparison-temperature-chart" class="preview-chart" viewBox="0 0 900 340"
              role="img" aria-label="Porównanie temperatur wybranych stacji"
            ></svg>
          </div>

          <h3 class="h6 mb-2">Kompletność, różnice i jakość</h3>
          <div class="comparison-table-wrap mb-3">
            <table class="table table-sm table-hover align-middle mb-0">
              <thead class="table-light sticky-top">
                <tr>
                  <th>Stacja</th>
                  <th>Od bazy</th>
                  <th>Średnia</th>
                  <th>Różnica</th>
                  <th>Kompletność</th>
                  <th>Braki</th>
                  <th>Typy</th>
                  <th>Jakość</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="comparison-summary-body"></tbody>
            </table>
          </div>

          <h3 class="h6 mb-2">Odległości pomiędzy stacjami</h3>
          <div class="table-responsive">
            <table class="table table-sm table-bordered text-center align-middle mb-0">
              <thead id="comparison-distance-head" class="table-light"></thead>
              <tbody id="comparison-distance-body"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="panel rounded-4 shadow-sm p-3 p-md-4">
        <h2 class="h5">Eksport danych NOAA do JSON</h2>
        <div class="row g-3 align-items-end">
          <div class="col-md-5">
            <label for="selected-station" class="form-label">Selected station</label>
            <input id="selected-station" class="form-control mono" readonly placeholder="Select a station above" />
          </div>
          <div class="col-md-3">
            <label for="temperature-export-mode" class="form-label">Rodzaj eksportu</label>
            <select id="temperature-export-mode" class="form-select">
              <option value="heatmap">Heatmapa (zgodność wsteczna)</option>
              <option value="daily">Dzienne</option>
              <option value="monthly">Miesięczne TMIN/TAVG/TAXN/TMAX</option>
              <option value="extended">Statystyki rozszerzone</option>
            </select>
          </div>
          <div class="col-md-2">
            <label for="start-year" class="form-label">Start year</label>
            <input id="start-year" type="number" min="1763" class="form-control" value="1973" />
          </div>
          <div class="col-md-2">
            <label for="end-year" class="form-label">End year</label>
            <input id="end-year" type="number" min="1763" class="form-control" />
          </div>
          <div class="col-md-4">
            <button id="temperature-json-btn" class="btn btn-success w-100" disabled>
              Download temperature JSON
            </button>
          </div>
          <div class="col-md-8">
            <div id="temperature-capabilities" class="small text-secondary">
              Wybierz stację, aby sprawdzić dostępne typy danych NOAA.
            </div>
          </div>
          <div class="col-12">
            <div id="temperature-message" class="small text-secondary">
              Tryb Heatmapa zachowuje dotychczasowy format JSON bez zmian.
            </div>
          </div>
        </div>
      </section>
    </main>

    <script nonce="{{CSP_NONCE}}" src="/static/vendor/leaflet/leaflet.js"></script>
    <script nonce="{{CSP_NONCE}}" src="/static/vendor/leaflet-markercluster/leaflet.markercluster.js"></script>
    <script nonce="{{CSP_NONCE}}">
      let lastResults = [];
      let qualityFilteredResults = [];
      let selectedStation = null;
      let stationTemperatureCapabilities = null;
      let previewAbortController = null;
      let comparisonAbortController = null;
      let lastComparisonPayloads = [];
      let visibleResultCount = 250;
      let stationMap = null;
      let stationTileLayer = null;
      let stationMarkerLayer = null;
      let countryBoundaryLayer = null;
      let boundaryAbortController = null;
      let activeBoundaryCountry = null;
      let mapTileErrorShown = false;
      const stationMarkersById = new Map();
      const stationCapabilitiesCache = new Map();
      const comparisonStations = new Map();
      const comparisonPayloadCache = new Map();

      const SUPPORTED_COUNTRY_OPTIONS = {{COUNTRY_OPTIONS_JSON}};
      const WORLD_MAP_CENTER = [20, 0];
      const WORLD_MAP_ZOOM = 2;
      const MAX_COMPARISON_STATIONS = 5;
      const COMPARISON_COLORS = ["#0d6efd", "#dc3545", "#198754", "#6f42c1", "#fd7e14"];

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

        const matched = SUPPORTED_COUNTRY_OPTIONS.find((item) => item.toLowerCase() === key);
        return matched || trimmed;
      }

      function initCountryAutocomplete() {
        const datalist = byId("country-options");
        datalist.innerHTML = "";
        for (const country of SUPPORTED_COUNTRY_OPTIONS) {
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

      function stationCoordinates(station) {
        if (
          station?.latitude === null || station?.latitude === undefined || station?.latitude === "" ||
          station?.longitude === null || station?.longitude === undefined || station?.longitude === ""
        ) return null;
        const latitude = Number(station?.latitude);
        const longitude = Number(station?.longitude);
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
        if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
        return { latitude, longitude };
      }

      function calculateStationQuality(
        station,
        availableDatatypes = null,
        observedCoveragePercent = null,
      ) {
        const verifiedCoverage = Number(observedCoveragePercent);
        const hasVerifiedCoverage = observedCoveragePercent !== null && Number.isFinite(verifiedCoverage);
        const rawCoverage = hasVerifiedCoverage
          ? verifiedCoverage / 100
          : Number(station?.datacoverage ?? 0);
        const coverage = Number.isFinite(rawCoverage)
          ? Math.min(1, Math.max(0, rawCoverage > 1 ? rawCoverage / 100 : rawCoverage))
          : 0;
        const startTime = Date.parse(String(station?.mindate || "").slice(0, 10));
        const endTime = Date.parse(String(station?.maxdate || "").slice(0, 10));
        const periodYears = Number.isFinite(startTime) && Number.isFinite(endTime) && endTime >= startTime
          ? Math.round(((endTime - startTime) / (365.2425 * 86400000)) * 10) / 10
          : 0;
        const endYear = Number.isFinite(endTime) ? new Date(endTime).getUTCFullYear() : null;
        const recencyYears = endYear === null ? null : Math.max(0, new Date().getUTCFullYear() - endYear);
        const recencyPoints = recencyYears === null ? 0 : recencyYears <= 2 ? 10 : recencyYears <= 5 ? 7 :
          recencyYears <= 10 ? 3 : 0;
        const verified = Array.isArray(availableDatatypes);
        const normalizedDatatypes = new Set(
          (availableDatatypes || []).map((datatype) => String(datatype).trim().toUpperCase()),
        );
        const coveragePoints = coverage * (verified ? 50 : 60);
        const periodPoints = Math.min(periodYears / 50, 1) * (verified ? 25 : 30);
        const datatypePoints = verified
          ? ["TMIN", "TAVG", "TMAX"].filter((datatype) => normalizedDatatypes.has(datatype)).length * 5
          : null;
        const score = Math.round(coveragePoints + periodPoints + recencyPoints + (datatypePoints || 0));
        let grade = "weak";
        let label = "słaba";
        if (score >= 75 && coverage >= 0.75 && periodYears >= 20) {
          grade = "good";
          label = "dobra";
        } else if (score >= 45 && coverage >= 0.4 && periodYears >= 5) {
          grade = "medium";
          label = "średnia";
        }
        return {
          score,
          grade,
          label,
          assessment: verified ? "verified" : "catalogue",
          coverage_percent: Math.round(coverage * 1000) / 10,
          period_years: periodYears,
          recency_years: recencyYears,
          available_datatypes: ["TMIN", "TAVG", "TMAX"].filter((datatype) => normalizedDatatypes.has(datatype)),
          components: {
            coverage: Math.round(coveragePoints * 10) / 10,
            period: Math.round(periodPoints * 10) / 10,
            recency: recencyPoints,
            datatypes: datatypePoints,
          },
        };
      }

      function stationQuality(station) {
        if (!station.quality) station.quality = calculateStationQuality(station);
        return station.quality;
      }

      function updateVerifiedStationQuality(station, capabilities, observedCoveragePercent = null) {
        station.quality = calculateStationQuality(
          station,
          capabilities?.core_temperature_datatypes || [],
          observedCoveragePercent,
        );
        return station.quality;
      }

      function qualityBadge(station) {
        const quality = stationQuality(station);
        const badge = document.createElement("span");
        badge.className = `badge quality-badge ${quality.grade} ` +
          `${quality.assessment === "verified" ? "verified" : ""}`;
        badge.textContent = `${quality.label} ${quality.score}/100`;
        badge.title = quality.assessment === "verified"
          ? `Ocena potwierdzona typami: ${quality.available_datatypes.join(", ") || "brak"}`
          : "Ocena wstępna z katalogu NOAA; typy temperatury nie zostały jeszcze sprawdzone";
        return badge;
      }

      function currentQualityFilters() {
        return {
          minYears: Math.max(0, Number(byId("quality-min-years").value) || 0),
          minCoverage: Math.min(100, Math.max(0, Number(byId("quality-min-coverage").value) || 0)),
          grade: byId("quality-grade-filter").value,
        };
      }

      function filteredStations() {
        const filters = currentQualityFilters();
        return lastResults.filter((station) => {
          const quality = stationQuality(station);
          return quality.period_years >= filters.minYears &&
            quality.coverage_percent >= filters.minCoverage &&
            (filters.grade === "all" || quality.grade === filters.grade);
        });
      }

      function updateQualitySummary(rows) {
        const counts = { good: 0, medium: 0, weak: 0 };
        for (const station of rows) counts[stationQuality(station).grade] += 1;
        byId("quality-good-count").textContent = String(counts.good);
        byId("quality-medium-count").textContent = String(counts.medium);
        byId("quality-weak-count").textContent = String(counts.weak);
        const verified = rows.filter((station) => stationQuality(station).assessment === "verified").length;
        byId("quality-filter-summary").textContent = lastResults.length
          ? `Widoczne ${rows.length} z ${lastResults.length}: dobre ${counts.good}, średnie ${counts.medium}, ` +
            `słabe ${counts.weak}. Ocena potwierdzona typami dla ${verified} stacji.`
          : "Wyszukaj stacje, aby zastosować filtry jakości.";
      }

      function applyQualityFilters({ fitMap = false } = {}) {
        qualityFilteredResults = filteredStations();
        visibleResultCount = 250;
        renderResults(qualityFilteredResults);
        renderStationMap(qualityFilteredResults);
        updateQualitySummary(qualityFilteredResults);
        byId("best-station-btn").disabled = qualityFilteredResults.length === 0;
        byId("export-json-btn").disabled = qualityFilteredResults.length === 0;
        byId("export-csv-btn").disabled = qualityFilteredResults.length === 0;
        if (fitMap) {
          if (!fitMapToStations(qualityFilteredResults)) resetMapView();
        }
        if (selectedStation) renderNearbyQualityComparison(selectedStation);
      }

      function stationIcon(qualityGrade = "weak", isSelected = false, isCompared = false) {
        return L.divIcon({
          className: `station-map-dot quality-${qualityGrade}` +
            `${isSelected ? " selected" : ""}${isCompared ? " comparison" : ""}`,
          html: '<span aria-hidden="true"></span>',
          iconSize: [18, 18],
          iconAnchor: [9, 9],
          popupAnchor: [0, -9],
        });
      }

      function stationClusterIcon(cluster) {
        const count = cluster.getChildCount();
        const gradeCounts = { good: 0, medium: 0, weak: 0 };
        for (const marker of cluster.getAllChildMarkers()) {
          const grade = marker.options.stationQualityGrade || "weak";
          gradeCounts[grade] = (gradeCounts[grade] || 0) + 1;
        }
        const dominantGrade = Object.entries(gradeCounts)
          .sort((left, right) => right[1] - left[1])[0][0];
        const sizeClass = count >= 100 ? "size-large" : count >= 20 ? "size-medium" : "size-small";
        const diameter = count >= 100 ? 50 : count >= 20 ? 44 : 38;
        return L.divIcon({
          className: `station-map-cluster ${sizeClass} quality-${dominantGrade}`,
          html: `<span>${count}</span>`,
          iconSize: [diameter, diameter],
        });
      }

      function stationPopup(station, coordinates) {
        const content = document.createElement("div");
        const title = document.createElement("div");
        title.className = "station-popup-title";
        title.textContent = station.name || station.city || station.station_id;
        const meta = document.createElement("div");
        meta.className = "station-popup-meta";
        meta.textContent = `${station.station_id} · ${coordinates.latitude.toFixed(4)}°, ` +
          `${coordinates.longitude.toFixed(4)}°`;
        const qualityLine = document.createElement("div");
        qualityLine.className = "mb-2";
        qualityLine.appendChild(qualityBadge(station));
        const selectButton = document.createElement("button");
        selectButton.type = "button";
        selectButton.className = "btn btn-sm btn-primary w-100";
        selectButton.textContent = "Wybierz tę stację";
        selectButton.addEventListener("click", () => selectStation(station));
        const compareButton = document.createElement("button");
        compareButton.type = "button";
        compareButton.className = "btn btn-sm btn-outline-info w-100 mt-2";
        compareButton.textContent = comparisonStations.has(String(station.station_id))
          ? "Usuń z porównania"
          : "Dodaj do porównania";
        compareButton.addEventListener("click", () => toggleComparisonStation(station));
        content.append(title, meta, qualityLine, selectButton, compareButton);
        return content;
      }

      function updateMapMetadata() {
        if (!stationMap) return;
        const center = stationMap.getCenter();
        const mapElement = byId("station-map");
        mapElement.dataset.zoom = String(stationMap.getZoom());
        mapElement.dataset.center = `${center.lat.toFixed(5)},${center.lng.toFixed(5)}`;
        byId("map-reset-btn").disabled = stationMap.getZoom() === WORLD_MAP_ZOOM &&
          Math.abs(center.lat - WORLD_MAP_CENTER[0]) < 0.01 &&
          Math.abs(center.lng - WORLD_MAP_CENTER[1]) < 0.01;
      }

      function ensureMapTiles() {
        if (!stationMap || !stationTileLayer || stationMap.hasLayer(stationTileLayer)) return;
        stationTileLayer.addTo(stationMap);
      }

      function initializeStationMap() {
        const loadingStatus = byId("map-loading-status");
        if (!window.L || typeof L.markerClusterGroup !== "function") {
          loadingStatus.classList.remove("d-none");
          loadingStatus.textContent = "Nie udało się uruchomić biblioteki mapy.";
          loadingStatus.classList.add("text-danger");
          byId("map-fit-btn").disabled = true;
          byId("map-reset-btn").disabled = true;
          return false;
        }

        stationMap = L.map("station-map", {
          minZoom: WORLD_MAP_ZOOM,
          maxZoom: 18,
          worldCopyJump: true,
          zoomControl: false,
          preferCanvas: true,
          zoomAnimation: false,
          fadeAnimation: false,
          markerZoomAnimation: false,
        });
        L.control.zoom({ position: "bottomright" }).addTo(stationMap);
        stationTileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          minZoom: WORLD_MAP_ZOOM,
          maxZoom: 19,
          updateWhenIdle: true,
          updateWhenZooming: false,
          keepBuffer: 1,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" ' +
            'target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
        });
        stationTileLayer.on("loading", () => {
          if (!mapTileErrorShown) {
            loadingStatus.textContent = "Ładowanie podkładu mapy…";
            loadingStatus.classList.remove("d-none", "text-danger");
          }
        });
        stationTileLayer.on("load", () => {
          if (!mapTileErrorShown) loadingStatus.classList.add("d-none");
        });
        stationTileLayer.on("tileerror", () => {
          if (mapTileErrorShown) return;
          mapTileErrorShown = true;
          loadingStatus.textContent = "Nie udało się pobrać podkładu. Sprawdź połączenie z internetem.";
          loadingStatus.classList.remove("d-none");
          loadingStatus.classList.add("text-danger");
        });

        stationMarkerLayer = L.markerClusterGroup({
          chunkedLoading: true,
          chunkInterval: 80,
          chunkDelay: 20,
          maxClusterRadius: 48,
          showCoverageOnHover: false,
          spiderfyOnMaxZoom: true,
          removeOutsideVisibleBounds: true,
          iconCreateFunction: stationClusterIcon,
        });
        stationMarkerLayer.addTo(stationMap);
        stationMap.on("moveend zoomend", updateMapMetadata);
        resetMapView();
        setTimeout(() => stationMap.invalidateSize(), 0);
        byId("station-map").dataset.mapReady = "true";
        return true;
      }

      function resetMapView({ loadTiles = false } = {}) {
        if (!stationMap) return;
        stationMap.setView(WORLD_MAP_CENTER, WORLD_MAP_ZOOM, { animate: false });
        if (loadTiles) ensureMapTiles();
        updateMapMetadata();
      }

      function clearCountryBoundary() {
        if (boundaryAbortController) boundaryAbortController.abort();
        boundaryAbortController = null;
        activeBoundaryCountry = null;
        if (countryBoundaryLayer && stationMap) stationMap.removeLayer(countryBoundaryLayer);
        countryBoundaryLayer = null;
        byId("map-country-boundary").textContent = "";
      }

      async function loadCountryBoundary(country) {
        const normalizedCountry = normalizeCountryInput(country);
        if (!normalizedCountry || !stationMap) {
          clearCountryBoundary();
          return;
        }
        if (activeBoundaryCountry === normalizedCountry && countryBoundaryLayer) return;
        if (boundaryAbortController) boundaryAbortController.abort();
        const controller = new AbortController();
        boundaryAbortController = controller;
        const requestedCountry = normalizedCountry;
        byId("map-country-boundary").textContent = `Ładowanie granic: ${requestedCountry}…`;
        try {
          const response = await fetch("/api/country-boundary", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: controller.signal,
            body: JSON.stringify({ country: requestedCountry }),
          });
          const responseData = await response.json();
          if (!response.ok) {
            throw new Error(responseData.message || "Nie udało się pobrać granic kraju");
          }
          if (controller.signal.aborted) return;
          if (countryBoundaryLayer) stationMap.removeLayer(countryBoundaryLayer);
          countryBoundaryLayer = L.geoJSON(responseData.data, {
            interactive: false,
            style: {
              color: "#0d6efd",
              weight: 1.5,
              opacity: 0.58,
              fillColor: "#0d6efd",
              fillOpacity: 0.12,
            },
          }).addTo(stationMap);
          countryBoundaryLayer.bringToBack();
          activeBoundaryCountry = requestedCountry;
          byId("map-country-boundary").textContent =
            `${requestedCountry}: delikatne niebieskie podświetlenie granic (12%).`;
        } catch (error) {
          if (error?.name === "AbortError") return;
          byId("map-country-boundary").textContent =
            `Nie udało się wyświetlić granic kraju ${requestedCountry}.`;
        } finally {
          if (boundaryAbortController === controller) boundaryAbortController = null;
        }
      }

      function fitMapToStations(rows = lastResults) {
        if (!stationMap) return false;
        const coordinates = rows
          .map((station) => stationCoordinates(station))
          .filter((point) => point !== null);
        if (coordinates.length === 0) return false;
        if (coordinates.length === 1) {
          stationMap.setView([coordinates[0].latitude, coordinates[0].longitude], 9, { animate: false });
          ensureMapTiles();
          return true;
        }
        const bounds = L.latLngBounds(
          coordinates.map((point) => [point.latitude, point.longitude]),
        );
        stationMap.fitBounds(bounds.pad(0.12), {
          padding: [30, 30],
          maxZoom: 9,
          animate: false,
        });
        ensureMapTiles();
        return true;
      }

      function focusMapOnStation(station, { scrollToMap = false } = {}) {
        const coordinates = stationCoordinates(station);
        if (!coordinates || !stationMap) {
          byId("map-selected-station").textContent = "Wybrana stacja nie ma prawidłowych współrzędnych.";
          return;
        }
        const marker = stationMarkersById.get(String(station.station_id));
        const showMarker = () => {
          stationMap.setView(
            [coordinates.latitude, coordinates.longitude],
            Math.max(stationMap.getZoom(), 11),
            { animate: false },
          );
          ensureMapTiles();
          if (marker) marker.openPopup();
        };
        if (marker && stationMarkerLayer) {
          stationMarkerLayer.zoomToShowLayer(marker, showMarker);
        } else {
          showMarker();
        }
        byId("map-selected-station").textContent =
          `${station.name || station.city || station.station_id} — ` +
          `${coordinates.latitude.toFixed(4)}°, ${coordinates.longitude.toFixed(4)}°`;
        if (scrollToMap) {
          byId("station-map-panel").scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }

      function updateMapSelection() {
        for (const [stationId, marker] of stationMarkersById.entries()) {
          const isSelected = selectedStation && stationId === String(selectedStation.station_id);
          const station = marker.options.stationRecord;
          const grade = station ? stationQuality(station).grade : "weak";
          marker.options.stationQualityGrade = grade;
          const isCompared = comparisonStations.has(stationId);
          marker.setIcon(stationIcon(grade, Boolean(isSelected), isCompared));
          if (station) marker.setPopupContent(stationPopup(station, stationCoordinates(station)));
          if (marker.getElement()) marker.getElement().setAttribute("aria-pressed", isSelected ? "true" : "false");
        }
        if (stationMarkerLayer) stationMarkerLayer.refreshClusters();
      }

      function renderStationMap(rows) {
        if (!stationMarkerLayer) {
          byId("map-summary").textContent = "Biblioteka mapy nie jest dostępna.";
          return;
        }
        stationMarkerLayer.clearLayers();
        stationMarkersById.clear();
        const mappable = rows.filter((row) => stationCoordinates(row) !== null);
        const markers = [];
        for (const station of mappable) {
          const coordinates = stationCoordinates(station);
          if (!coordinates) continue;
          const quality = stationQuality(station);
          const marker = L.marker([coordinates.latitude, coordinates.longitude], {
            icon: stationIcon(
              quality.grade,
              Boolean(selectedStation && String(station.station_id) === String(selectedStation.station_id)),
              comparisonStations.has(String(station.station_id)),
            ),
            stationRecord: station,
            stationQualityGrade: quality.grade,
            keyboard: true,
            title: station.name || station.city || station.station_id,
            alt: `Wybierz stację ${station.name || station.city || station.station_id}`,
            riseOnHover: true,
          });
          marker.bindPopup(stationPopup(station, coordinates));
          marker.on("click", () => selectStation(station));
          stationMarkersById.set(String(station.station_id), marker);
          markers.push(marker);
        }
        stationMarkerLayer.addLayers(markers);
        const omitted = rows.length - mappable.length;
        let summary = `Na mapie: wszystkie ${mappable.length} stacje ze współrzędnymi z bieżącego wyniku.`;
        if (omitted > 0) summary += ` Pominięto ${omitted} bez prawidłowych współrzędnych.`;
        if (rows.length === 0) summary = "Wyszukaj stacje, aby umieścić je na mapie.";
        byId("map-summary").textContent = summary;
        byId("map-fit-btn").disabled = mappable.length === 0;
        updateMapSelection();
      }

      function renderResults(rows) {
        const body = byId("result-body");
        body.replaceChildren();
        const visibleRows = rows.slice(0, visibleResultCount);
        for (const row of visibleRows) {
          const tr = document.createElement("tr");
          const isSelected = selectedStation && row.station_id === selectedStation.station_id;
          const quality = stationQuality(row);
          tr.classList.add(`station-row-${quality.grade}`);
          if (isSelected) tr.classList.add("table-active");
          for (const [value, className] of [
            [row.city, ""],
            [row.name, ""],
            [row.station_id, "mono"],
            [row.country, ""],
            [row.latitude, ""],
            [row.longitude, ""],
            [row.mindate, ""],
            [row.maxdate, ""],
            [`${quality.coverage_percent.toFixed(1)}%`, ""],
            [quality.period_years, ""],
          ]) {
            const cell = document.createElement("td");
            cell.textContent = value ?? "";
            if (className) cell.className = className;
            tr.appendChild(cell);
          }
          const qualityCell = document.createElement("td");
          qualityCell.appendChild(qualityBadge(row));
          tr.appendChild(qualityCell);
          const actionCell = document.createElement("td");
          const selectButton = document.createElement("button");
          selectButton.type = "button";
          selectButton.className = isSelected ? "btn btn-sm btn-primary" : "btn btn-sm btn-outline-primary";
          selectButton.textContent = "Wybierz";
          selectButton.addEventListener("click", () => selectStation(row, { scrollToMap: true }));
          actionCell.appendChild(selectButton);
          const compareButton = document.createElement("button");
          compareButton.type = "button";
          compareButton.className = comparisonStations.has(String(row.station_id))
            ? "btn btn-sm btn-info ms-1"
            : "btn btn-sm btn-outline-info ms-1";
          compareButton.textContent = comparisonStations.has(String(row.station_id)) ? "Dodano" : "Porównaj";
          compareButton.addEventListener("click", () => toggleComparisonStation(row));
          actionCell.appendChild(compareButton);
          tr.appendChild(actionCell);
          body.appendChild(tr);
        }
        const pagination = byId("result-pagination");
        const hasMore = visibleRows.length < rows.length;
        pagination.classList.toggle("d-none", rows.length <= 250);
        pagination.classList.toggle("d-flex", rows.length > 250);
        byId("result-pagination-text").textContent = `Pokazano ${visibleRows.length} z ${rows.length}.`;
        byId("show-more-btn").classList.toggle("d-none", !hasMore);
      }

      function setExportModeAvailability(capabilities) {
        const modeSelect = byId("temperature-export-mode");
        const modes = capabilities?.export_modes || {};
        for (const option of modeSelect.options) {
          option.disabled = modes[option.value] === false;
        }
        if (modeSelect.selectedOptions[0]?.disabled) {
          const fallback = Array.from(modeSelect.options).find((option) => !option.disabled);
          if (fallback) modeSelect.value = fallback.value;
        }
      }

      async function getStationCapabilities(station) {
        const stationId = String(station.station_id);
        if (stationCapabilitiesCache.has(stationId)) {
          return await stationCapabilitiesCache.get(stationId);
        }
        const request = (async () => {
          const response = await fetch("/api/temperature-capabilities", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ station_id: stationId }),
          });
          const responseData = await response.json();
          if (!response.ok) {
            throw new Error(
              `${responseData.code ?? "ERROR"}: ` +
              `${responseData.message ?? "Nie udało się sprawdzić typów danych"}`,
            );
          }
          return responseData.data;
        })();
        stationCapabilitiesCache.set(stationId, request);
        try {
          return await request;
        } catch (error) {
          stationCapabilitiesCache.delete(stationId);
          throw error;
        }
      }

      async function checkTemperatureCapabilities(station) {
        stationTemperatureCapabilities = null;
        byId("temperature-json-btn").disabled = true;
        byId("temperature-capabilities").textContent = "Sprawdzanie typów danych dla wybranej stacji...";
        try {
          const capabilities = await getStationCapabilities(station);
          updateVerifiedStationQuality(station, capabilities);
          applyQualityFilters();
          renderPreviewQuality(station, capabilities);
          renderNearbyQualityComparison(station);
          if (!selectedStation || String(selectedStation.station_id) !== String(station.station_id)) return;
          stationTemperatureCapabilities = capabilities;
          setExportModeAvailability(stationTemperatureCapabilities);
          const reported = stationTemperatureCapabilities.core_temperature_datatypes || [];
          const derived = Object.entries(stationTemperatureCapabilities.derived_datatypes || {})
            .filter(([, available]) => available)
            .map(([datatype]) => `${datatype} (obliczane)`);
          const labels = [...reported, ...derived];
          byId("temperature-capabilities").textContent = labels.length
            ? `Dostępne: ${labels.join(", ")}. TAVG jest wartością NOAA; TAXN ma wzór (TMAX + TMIN) / 2.`
            : "Stacja nie udostępnia obsługiwanych danych temperatury.";
          byId("temperature-json-btn").disabled = !Object.values(
            stationTemperatureCapabilities.export_modes || {},
          ).some(Boolean);
        } catch (error) {
          if (!selectedStation || String(selectedStation.station_id) !== String(station.station_id)) return;
          byId("temperature-capabilities").textContent = `Nie udało się sprawdzić typów danych: ${error}`;
        }
      }

      function renderPreviewQuality(station, capabilities = null) {
        const quality = capabilities
          ? updateVerifiedStationQuality(station, capabilities)
          : stationQuality(station);
        byId("preview-quality").textContent = `${quality.label} ${quality.score}/100`;
        byId("preview-quality").className = `preview-stat-value text-${
          quality.grade === "good" ? "success" : quality.grade === "medium" ? "warning" : "danger"
        }`;
        byId("preview-quality-detail").textContent =
          `${quality.coverage_percent.toFixed(1)}% · ${quality.period_years.toFixed(1)} lat · ` +
          (quality.assessment === "verified" ? "typy potwierdzone" : "ocena wstępna");
        const datatypes = capabilities?.core_temperature_datatypes || quality.available_datatypes || [];
        byId("preview-datatypes").textContent = datatypes.length ? datatypes.join(" / ") : "sprawdzanie…";
      }

      function svgElement(name, attributes = {}, textContent = null) {
        const node = document.createElementNS("http://www.w3.org/2000/svg", name);
        for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
        if (textContent !== null) node.textContent = String(textContent);
        return node;
      }

      function flattenMonthlyValues(payload, datatype) {
        const matrix = payload?.temperatures?.[datatype] || [];
        const points = [];
        for (let yearIndex = 0; yearIndex < (payload?.years || []).length; yearIndex += 1) {
          for (let monthIndex = 0; monthIndex < 12; monthIndex += 1) {
            const value = matrix?.[yearIndex]?.[monthIndex];
            points.push({
              label: `${payload.years[yearIndex]}-${String(monthIndex + 1).padStart(2, "0")}`,
              value: Number.isFinite(Number(value)) && value !== null ? Number(value) : null,
            });
          }
        }
        return points;
      }

      function clearChart(svg, emptyText) {
        svg.replaceChildren();
        svg.appendChild(svgElement("text", {
          x: 450,
          y: Number(svg.getAttribute("viewBox").split(" ")[3]) / 2,
          "text-anchor": "middle",
          class: "chart-axis-label",
        }, emptyText));
      }

      function renderTemperatureChart(payload) {
        const svg = byId("temperature-preview-chart");
        svg.replaceChildren();
        const series = [
          { datatype: "TMIN", className: "series-tmin" },
          { datatype: "TAVG", className: "series-tavg" },
          { datatype: "TMAX", className: "series-tmax" },
        ].map((item) => ({ ...item, points: flattenMonthlyValues(payload, item.datatype) }));
        const numericValues = series.flatMap((item) => item.points)
          .map((point) => point.value)
          .filter((value) => value !== null);
        const pointCount = series[0]?.points.length || 0;
        if (!numericValues.length || pointCount === 0) {
          clearChart(svg, "Brak danych temperatury w wybranym okresie.");
          return;
        }

        const width = 900;
        const height = 320;
        const margin = { top: 18, right: 22, bottom: 45, left: 55 };
        const innerWidth = width - margin.left - margin.right;
        const innerHeight = height - margin.top - margin.bottom;
        const rawMin = Math.min(...numericValues);
        const rawMax = Math.max(...numericValues);
        const yMin = Math.floor(rawMin - 2);
        const yMax = Math.ceil(rawMax + 2) || yMin + 1;
        const xFor = (index) => margin.left + (
          pointCount === 1 ? innerWidth / 2 : index * innerWidth / (pointCount - 1)
        );
        const yFor = (value) => margin.top + (yMax - value) * innerHeight / Math.max(yMax - yMin, 1);

        for (let index = 0; index <= 5; index += 1) {
          const value = yMin + (yMax - yMin) * index / 5;
          const y = yFor(value);
          svg.appendChild(svgElement("line", {
            x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "chart-grid",
          }));
          svg.appendChild(svgElement("text", {
            x: margin.left - 8, y: y + 4, "text-anchor": "end", class: "chart-axis-label",
          }, `${value.toFixed(0)}°C`));
        }
        const labelEvery = Math.max(1, Math.ceil(pointCount / 8));
        for (let index = 0; index < pointCount; index += labelEvery) {
          svg.appendChild(svgElement("text", {
            x: xFor(index), y: height - 16, "text-anchor": "middle", class: "chart-axis-label",
          }, series[0].points[index].label));
        }

        for (const item of series) {
          let segment = [];
          const drawSegment = () => {
            if (!segment.length) return;
            const pathData = segment.map((point, index) =>
              `${index === 0 ? "M" : "L"}${xFor(point.index).toFixed(2)},${yFor(point.value).toFixed(2)}`,
            ).join(" ");
            svg.appendChild(svgElement("path", { d: pathData, class: `chart-line ${item.className}` }));
            segment = [];
          };
          item.points.forEach((point, index) => {
            if (point.value === null) {
              drawSegment();
              return;
            }
            segment.push({ index, value: point.value });
            const circle = svgElement("circle", {
              cx: xFor(index), cy: yFor(point.value), r: 3, class: `chart-point ${item.className}`,
            });
            circle.appendChild(svgElement(
              "title",
              {},
              `${point.label} · ${item.datatype}: ${point.value.toFixed(2)}°C`,
            ));
            svg.appendChild(circle);
          });
          drawSegment();
        }
      }

      function renderAmplitudeChart(payload) {
        const svg = byId("amplitude-preview-chart");
        svg.replaceChildren();
        const points = flattenMonthlyValues(payload, "AMPLITUDE");
        const numericValues = points.map((point) => point.value).filter((value) => value !== null);
        if (!numericValues.length) {
          clearChart(svg, "Amplituda wymaga jednoczesnych danych TMIN i TMAX.");
          return;
        }
        const width = 900;
        const height = 180;
        const margin = { top: 15, right: 20, bottom: 42, left: 50 };
        const innerWidth = width - margin.left - margin.right;
        const innerHeight = height - margin.top - margin.bottom;
        const yMax = Math.max(1, Math.ceil(Math.max(...numericValues) + 1));
        const slotWidth = innerWidth / Math.max(points.length, 1);
        const barWidth = Math.max(2, slotWidth * 0.72);
        for (let index = 0; index <= 3; index += 1) {
          const value = yMax * index / 3;
          const y = margin.top + innerHeight - value * innerHeight / yMax;
          svg.appendChild(svgElement("line", {
            x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "chart-grid",
          }));
          svg.appendChild(svgElement("text", {
            x: margin.left - 7, y: y + 4, "text-anchor": "end", class: "chart-axis-label",
          }, `${value.toFixed(0)}°C`));
        }
        points.forEach((point, index) => {
          if (point.value === null) return;
          const barHeight = point.value * innerHeight / yMax;
          const rect = svgElement("rect", {
            x: margin.left + index * slotWidth + (slotWidth - barWidth) / 2,
            y: margin.top + innerHeight - barHeight,
            width: barWidth,
            height: barHeight,
            rx: 2,
            class: "chart-bar",
          });
          rect.appendChild(svgElement("title", {}, `${point.label} · amplituda: ${point.value.toFixed(2)}°C`));
          svg.appendChild(rect);
        });
        const labelEvery = Math.max(1, Math.ceil(points.length / 8));
        for (let index = 0; index < points.length; index += labelEvery) {
          svg.appendChild(svgElement("text", {
            x: margin.left + (index + 0.5) * slotWidth,
            y: height - 15,
            "text-anchor": "middle",
            class: "chart-axis-label",
          }, points[index].label));
        }
      }

      function renderPreviewCompleteness(payload) {
        const observed = (payload.observed_datatypes || [])
          .filter((datatype) => ["TMIN", "TAVG", "TMAX"].includes(datatype));
        const expectedMatrix = payload?.completeness?.expected_days || [];
        const percentMatrices = payload?.completeness?.percent || {};
        const missingByType = {};
        for (const datatype of observed) {
          let expectedTotal = 0;
          let observedTotal = 0;
          for (let yearIndex = 0; yearIndex < expectedMatrix.length; yearIndex += 1) {
            for (let monthIndex = 0; monthIndex < 12; monthIndex += 1) {
              const expected = Number(expectedMatrix?.[yearIndex]?.[monthIndex]) || 0;
              const percent = Number(percentMatrices?.[datatype]?.[yearIndex]?.[monthIndex]) || 0;
              expectedTotal += expected;
              observedTotal += Math.round(expected * percent / 100);
            }
          }
          missingByType[datatype] = Math.max(0, expectedTotal - observedTotal);
        }
        const primaryType = observed.includes("TAVG") ? "TAVG" : observed[0];
        byId("preview-missing-days").textContent = primaryType
          ? `${primaryType}: ${missingByType[primaryType]}`
          : "brak danych";
        byId("preview-missing-detail").textContent = observed.length
          ? observed.map((datatype) => `${datatype} ${missingByType[datatype]}`).join(" · ")
          : "NOAA nie zwróciła obsługiwanych typów";

        const incompleteYears = [];
        for (let yearIndex = 0; yearIndex < (payload.years || []).length; yearIndex += 1) {
          const expected = (expectedMatrix[yearIndex] || []).reduce((sum, value) => sum + (Number(value) || 0), 0);
          if (!expected || !observed.length) {
            incompleteYears.push(payload.years[yearIndex]);
            continue;
          }
          const minimumCompleteness = Math.min(...observed.map((datatype) => {
            const weightedObserved = (expectedMatrix[yearIndex] || []).reduce((sum, days, monthIndex) => {
              const percent = Number(percentMatrices?.[datatype]?.[yearIndex]?.[monthIndex]) || 0;
              return sum + (Number(days) || 0) * percent / 100;
            }, 0);
            return weightedObserved * 100 / expected;
          }));
          if (minimumCompleteness < 90) incompleteYears.push(payload.years[yearIndex]);
        }
        byId("preview-incomplete-years").textContent = String(incompleteYears.length);
        byId("preview-incomplete-detail").textContent = incompleteYears.length
          ? incompleteYears.slice(0, 8).join(", ") + (incompleteYears.length > 8 ? "…" : "")
          : "Wszystkie lata mają co najmniej 90%";
      }

      function previewYearRange(station) {
        const today = new Date();
        const lastCompleteYear = today.getUTCFullYear() - 1;
        const stationEndYear = Number(String(station.maxdate || "").slice(0, 4));
        const stationStartYear = Number(String(station.mindate || "").slice(0, 4));
        const endYear = Number.isFinite(stationEndYear)
          ? Math.min(stationEndYear, lastCompleteYear)
          : lastCompleteYear;
        const requestedYears = Math.max(1, Number(byId("preview-years").value) || 3);
        return {
          startYear: Math.max(
            Number.isFinite(stationStartYear) ? stationStartYear : 1763,
            endYear - requestedYears + 1,
          ),
          endYear,
        };
      }

      async function loadTemperaturePreview(station) {
        if (previewAbortController) previewAbortController.abort();
        const controller = new AbortController();
        previewAbortController = controller;
        const stationId = String(station.station_id);
        const range = previewYearRange(station);
        byId("preview-empty").classList.add("d-none");
        byId("preview-content").classList.remove("d-none");
        byId("preview-refresh-btn").disabled = true;
        byId("preview-status").textContent =
          `Pobieranie podglądu ${range.startYear}–${range.endYear} dla ${station.name || station.city}…`;
        renderPreviewQuality(station);
        clearChart(byId("temperature-preview-chart"), "Pobieranie danych NOAA…");
        clearChart(byId("amplitude-preview-chart"), "Pobieranie danych NOAA…");
        try {
          const response = await fetch("/api/temperatures", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: controller.signal,
            body: JSON.stringify({
              station_id: stationId,
              start_year: range.startYear,
              end_year: range.endYear,
              mode: "monthly",
            }),
          });
          const responseData = await response.json();
          if (!response.ok) {
            throw new Error(`${responseData.code ?? "ERROR"}: ${responseData.message ?? "Błąd podglądu"}`);
          }
          if (!selectedStation || String(selectedStation.station_id) !== stationId) return;
          const payload = responseData.data;
          const observedDatatypes = payload.observed_datatypes || [];
          updateVerifiedStationQuality(
            station,
            { core_temperature_datatypes: observedDatatypes },
            payloadCoreCompleteness(payload),
          );
          renderPreviewQuality(station);
          renderResults(qualityFilteredResults);
          updateMapSelection();
          renderNearbyQualityComparison(station);
          renderTemperatureChart(payload);
          renderAmplitudeChart(payload);
          renderPreviewCompleteness(payload);
          byId("preview-datatypes").textContent = (payload.observed_datatypes || []).join(" / ") || "brak";
          byId("preview-status").textContent =
            `Podgląd ${payload.period.start_date}–${payload.period.end_date}. ` +
            "TAVG jest wartością NOAA; amplituda to TMAX − TMIN.";
        } catch (error) {
          if (error?.name === "AbortError") return;
          if (!selectedStation || String(selectedStation.station_id) !== stationId) return;
          byId("preview-status").textContent = `Nie udało się pobrać podglądu: ${error}`;
          clearChart(byId("temperature-preview-chart"), "Nie udało się pobrać podglądu.");
          clearChart(byId("amplitude-preview-chart"), "Nie udało się pobrać podglądu.");
        } finally {
          if (previewAbortController === controller) previewAbortController = null;
          if (selectedStation && String(selectedStation.station_id) === stationId) {
            byId("preview-refresh-btn").disabled = false;
          }
        }
      }

      function haversineDistanceKm(left, right) {
        const leftPoint = stationCoordinates(left);
        const rightPoint = stationCoordinates(right);
        if (!leftPoint || !rightPoint) return Number.POSITIVE_INFINITY;
        const radians = (degrees) => degrees * Math.PI / 180;
        const deltaLat = radians(rightPoint.latitude - leftPoint.latitude);
        const deltaLon = radians(rightPoint.longitude - leftPoint.longitude);
        const lat1 = radians(leftPoint.latitude);
        const lat2 = radians(rightPoint.latitude);
        const a = Math.sin(deltaLat / 2) ** 2 +
          Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
        return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      }

      function renderNearbyQualityComparison(station) {
        const body = byId("nearby-quality-body");
        if (!body) return;
        body.replaceChildren();
        const selectedId = String(station.station_id);
        const nearby = [station, ...lastResults
          .filter((candidate) => String(candidate.station_id) !== selectedId)
          .map((candidate) => ({ candidate, distance: haversineDistanceKm(station, candidate) }))
          .filter((entry) => Number.isFinite(entry.distance))
          .sort((left, right) => left.distance - right.distance)
          .slice(0, 4)
          .map((entry) => entry.candidate)];
        for (const candidate of nearby) {
          const quality = stationQuality(candidate);
          const distance = haversineDistanceKm(station, candidate);
          const row = document.createElement("tr");
          if (String(candidate.station_id) === selectedId) row.classList.add("table-active");
          const values = [
            candidate.name || candidate.city || candidate.station_id,
            String(candidate.station_id) === selectedId ? "wybrana" : `${distance.toFixed(1)} km`,
            `${quality.coverage_percent.toFixed(1)}%`,
            quality.period_years.toFixed(1),
            quality.assessment === "verified"
              ? quality.available_datatypes.join("/") || "brak"
              : "niesprawdzone",
          ];
          for (const value of values) {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.appendChild(cell);
          }
          const qualityCell = document.createElement("td");
          qualityCell.appendChild(qualityBadge(candidate));
          row.appendChild(qualityCell);
          const actionCell = document.createElement("td");
          if (String(candidate.station_id) !== selectedId) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-sm btn-outline-primary";
            button.textContent = "Wybierz";
            button.addEventListener("click", () => selectStation(candidate, { scrollToMap: true }));
            actionCell.appendChild(button);
          }
          const compareButton = document.createElement("button");
          compareButton.type = "button";
          compareButton.className = comparisonStations.has(String(candidate.station_id))
            ? "btn btn-sm btn-info ms-1"
            : "btn btn-sm btn-outline-info ms-1";
          compareButton.textContent = comparisonStations.has(String(candidate.station_id)) ? "Dodano" : "Porównaj";
          compareButton.addEventListener("click", () => toggleComparisonStation(candidate));
          actionCell.appendChild(compareButton);
          row.appendChild(actionCell);
          body.appendChild(row);
        }
      }

      function stationDisplayName(station) {
        return station.name || station.city || station.station_id;
      }

      function comparisonCommonRange() {
        const stations = Array.from(comparisonStations.values());
        const lastCompleteYear = new Date().getUTCFullYear() - 1;
        const startYears = stations.map((station) => Number(String(station.mindate || "").slice(0, 4)));
        const endYears = stations.map((station) => Number(String(station.maxdate || "").slice(0, 4)));
        if (startYears.some((year) => !Number.isFinite(year)) || endYears.some((year) => !Number.isFinite(year))) {
          return null;
        }
        const commonStart = Math.max(...startYears);
        const commonEnd = Math.min(...endYears, lastCompleteYear);
        if (commonStart > commonEnd) return null;
        const requestedYears = Math.max(1, Number(byId("comparison-years").value) || 5);
        return {
          commonStart,
          commonEnd,
          activeStart: Math.max(commonStart, commonEnd - requestedYears + 1),
          activeEnd: commonEnd,
        };
      }

      function renderComparisonSelection() {
        const selection = byId("comparison-selection");
        selection.replaceChildren();
        const stations = Array.from(comparisonStations.values());
        byId("comparison-count").textContent = `${stations.length}/${MAX_COMPARISON_STATIONS}`;
        byId("comparison-clear-btn").disabled = stations.length === 0;
        byId("comparison-refresh-btn").disabled = stations.length < 2;
        if (!stations.length) {
          const empty = document.createElement("span");
          empty.className = "small text-secondary";
          empty.textContent = "Nie wybrano jeszcze żadnej stacji.";
          selection.appendChild(empty);
        } else {
          stations.forEach((station, index) => {
            const chip = document.createElement("span");
            chip.className = "comparison-chip";
            const colorKey = document.createElement("span");
            colorKey.className = "comparison-color-key";
            colorKey.style.background = COMPARISON_COLORS[index];
            const label = document.createElement("span");
            label.textContent = stationDisplayName(station);
            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.textContent = "×";
            removeButton.setAttribute("aria-label", `Usuń ${stationDisplayName(station)} z porównania`);
            removeButton.addEventListener("click", () => toggleComparisonStation(station));
            chip.append(colorKey, label);
            if (index === 0) {
              const baseBadge = document.createElement("span");
              baseBadge.className = "badge comparison-base-badge";
              baseBadge.textContent = "baza";
              chip.appendChild(baseBadge);
            }
            chip.appendChild(removeButton);
            selection.appendChild(chip);
          });
        }
        renderResults(qualityFilteredResults);
        updateMapSelection();
        if (selectedStation) renderNearbyQualityComparison(selectedStation);
      }

      function clearComparisonResults(message = "Wybierz co najmniej dwie stacje.") {
        lastComparisonPayloads = [];
        byId("comparison-results").classList.add("d-none");
        byId("comparison-empty").classList.remove("d-none");
        byId("comparison-empty").textContent = message;
        clearChart(byId("comparison-temperature-chart"), message);
      }

      function toggleComparisonStation(station) {
        const stationId = String(station.station_id);
        if (comparisonStations.has(stationId)) {
          comparisonStations.delete(stationId);
        } else {
          if (comparisonStations.size >= MAX_COMPARISON_STATIONS) {
            byId("comparison-status").textContent = "Można porównać maksymalnie 5 stacji.";
            return;
          }
          comparisonStations.set(stationId, station);
        }
        if (comparisonAbortController) comparisonAbortController.abort();
        renderComparisonSelection();
        if (comparisonStations.size < 2) {
          byId("comparison-status").textContent = comparisonStations.size
            ? "Dodaj jeszcze jedną stację, aby rozpocząć porównanie."
            : "Dodaj od 2 do 5 stacji przyciskiem „Porównaj”.";
          clearComparisonResults(
            "Wybierz co najmniej dwie stacje. Pierwsza dodana stacja będzie bazą dla różnic.",
          );
          return;
        }
        void loadStationComparison();
      }

      async function fetchComparisonPayload(station, range, signal) {
        const cacheKey = `${station.station_id}:${range.activeStart}:${range.activeEnd}`;
        if (comparisonPayloadCache.has(cacheKey)) return comparisonPayloadCache.get(cacheKey);
        const response = await fetch("/api/temperatures", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal,
          body: JSON.stringify({
            station_id: station.station_id,
            start_year: range.activeStart,
            end_year: range.activeEnd,
            mode: "monthly",
          }),
        });
        const responseData = await response.json();
        if (!response.ok) {
          throw new Error(`${responseData.code ?? "ERROR"}: ${responseData.message ?? "Błąd NOAA"}`);
        }
        comparisonPayloadCache.set(cacheKey, responseData.data);
        return responseData.data;
      }

      function comparisonMetrics(payload, datatype) {
        const values = flattenMonthlyValues(payload, datatype)
          .map((point) => point.value)
          .filter((value) => value !== null);
        const expectedMatrix = payload?.completeness?.expected_days || [];
        const percentMatrix = payload?.completeness?.percent?.[datatype] || [];
        let expectedDays = 0;
        let observedDays = 0;
        for (let yearIndex = 0; yearIndex < expectedMatrix.length; yearIndex += 1) {
          for (let monthIndex = 0; monthIndex < 12; monthIndex += 1) {
            const expected = Number(expectedMatrix?.[yearIndex]?.[monthIndex]) || 0;
            const percent = Number(percentMatrix?.[yearIndex]?.[monthIndex]) || 0;
            expectedDays += expected;
            observedDays += expected * percent / 100;
          }
        }
        return {
          average: values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null,
          completeness: expectedDays ? observedDays * 100 / expectedDays : 0,
          missingDays: Math.max(0, Math.round(expectedDays - observedDays)),
          observations: values.length,
        };
      }

      function payloadCoreCompleteness(payload) {
        const datatypes = (payload.observed_datatypes || [])
          .filter((datatype) => ["TMIN", "TAVG", "TMAX"].includes(datatype));
        if (!datatypes.length) return 0;
        const values = datatypes.map((datatype) => comparisonMetrics(payload, datatype).completeness);
        return values.reduce((sum, value) => sum + value, 0) / values.length;
      }

      function renderComparisonTemperatureChart(entries, datatype) {
        const svg = byId("comparison-temperature-chart");
        svg.replaceChildren();
        const series = entries.map((entry, index) => ({
          ...entry,
          color: COMPARISON_COLORS[index],
          points: flattenMonthlyValues(entry.payload, datatype),
        }));
        const numericValues = series.flatMap((item) => item.points)
          .map((point) => point.value)
          .filter((value) => value !== null);
        const pointCount = series[0]?.points.length || 0;
        if (!numericValues.length || pointCount === 0) {
          clearChart(svg, `Brak wspólnych danych ${datatype} dla wybranych stacji.`);
          return;
        }
        const width = 900;
        const height = 340;
        const margin = { top: 18, right: 22, bottom: 45, left: 55 };
        const innerWidth = width - margin.left - margin.right;
        const innerHeight = height - margin.top - margin.bottom;
        const yMin = Math.floor(Math.min(...numericValues) - 2);
        const yMax = Math.ceil(Math.max(...numericValues) + 2) || yMin + 1;
        const xFor = (index) => margin.left + (
          pointCount === 1 ? innerWidth / 2 : index * innerWidth / (pointCount - 1)
        );
        const yFor = (value) => margin.top + (yMax - value) * innerHeight / Math.max(yMax - yMin, 1);
        for (let index = 0; index <= 5; index += 1) {
          const value = yMin + (yMax - yMin) * index / 5;
          const y = yFor(value);
          svg.appendChild(svgElement("line", {
            x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "chart-grid",
          }));
          svg.appendChild(svgElement("text", {
            x: margin.left - 8, y: y + 4, "text-anchor": "end", class: "chart-axis-label",
          }, `${value.toFixed(0)}°C`));
        }
        const labelEvery = Math.max(1, Math.ceil(pointCount / 8));
        for (let index = 0; index < pointCount; index += labelEvery) {
          svg.appendChild(svgElement("text", {
            x: xFor(index), y: height - 16, "text-anchor": "middle", class: "chart-axis-label",
          }, series[0].points[index].label));
        }
        for (const item of series) {
          let segment = [];
          const drawSegment = () => {
            if (!segment.length) return;
            const pathData = segment.map((point, index) =>
              `${index === 0 ? "M" : "L"}${xFor(point.index).toFixed(2)},${yFor(point.value).toFixed(2)}`,
            ).join(" ");
            svg.appendChild(svgElement("path", {
              d: pathData,
              fill: "none",
              stroke: item.color,
              "stroke-width": 2.5,
              "stroke-linejoin": "round",
              "stroke-linecap": "round",
            }));
            segment = [];
          };
          item.points.forEach((point, index) => {
            if (point.value === null) {
              drawSegment();
              return;
            }
            segment.push({ index, value: point.value });
            const circle = svgElement("circle", {
              cx: xFor(index), cy: yFor(point.value), r: 2.8,
              fill: item.color, stroke: "#ffffff", "stroke-width": 1.2,
            });
            circle.appendChild(svgElement(
              "title",
              {},
              `${stationDisplayName(item.station)} · ${point.label}: ${point.value.toFixed(2)}°C`,
            ));
            svg.appendChild(circle);
          });
          drawSegment();
        }
      }

      function renderComparisonLegend(entries) {
        const legend = byId("comparison-chart-legend");
        legend.replaceChildren();
        entries.forEach((entry, index) => {
          const item = document.createElement("span");
          item.className = "d-inline-flex align-items-center gap-1";
          const key = document.createElement("span");
          key.className = "chart-legend-line";
          key.style.background = COMPARISON_COLORS[index];
          const label = document.createElement("span");
          label.textContent = stationDisplayName(entry.station);
          item.append(key, label);
          legend.appendChild(item);
        });
      }

      function renderComparisonSummary(entries, datatype) {
        const body = byId("comparison-summary-body");
        body.replaceChildren();
        const baseMetrics = comparisonMetrics(entries[0].payload, datatype);
        const rows = entries.map((entry, index) => ({
          ...entry,
          index,
          metrics: comparisonMetrics(entry.payload, datatype),
        }));
        rows.forEach((entry) => {
          const quality = stationQuality(entry.station);
          const row = document.createElement("tr");
          const nameCell = document.createElement("td");
          const key = document.createElement("span");
          key.className = "comparison-color-key me-1";
          key.style.background = COMPARISON_COLORS[entry.index];
          nameCell.appendChild(key);
          nameCell.append(document.createTextNode(stationDisplayName(entry.station)));
          if (entry.index === 0) {
            const badge = document.createElement("span");
            badge.className = "badge comparison-base-badge ms-1";
            badge.textContent = "baza";
            nameCell.appendChild(badge);
          }
          row.appendChild(nameCell);
          const distance = entry.index === 0 ? 0 : haversineDistanceKm(entries[0].station, entry.station);
          const difference = entry.metrics.average !== null && baseMetrics.average !== null
            ? entry.metrics.average - baseMetrics.average
            : null;
          const values = [
            entry.index === 0 ? "—" : `${distance.toFixed(1)} km`,
            entry.metrics.average === null ? "brak" : `${entry.metrics.average.toFixed(2)}°C`,
            difference === null ? "brak" : `${difference >= 0 ? "+" : ""}${difference.toFixed(2)}°C`,
            `${entry.metrics.completeness.toFixed(1)}%`,
            entry.metrics.missingDays,
            (entry.payload.observed_datatypes || []).join("/") || "brak",
          ];
          values.forEach((value) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.appendChild(cell);
          });
          const qualityCell = document.createElement("td");
          qualityCell.appendChild(qualityBadge(entry.station));
          row.appendChild(qualityCell);
          const actionCell = document.createElement("td");
          const selectButton = document.createElement("button");
          selectButton.type = "button";
          selectButton.className = "btn btn-sm btn-outline-primary";
          selectButton.textContent = "Wybierz";
          selectButton.addEventListener("click", () => selectStation(entry.station, { scrollToMap: true }));
          actionCell.appendChild(selectButton);
          row.appendChild(actionCell);
          body.appendChild(row);
        });
        return rows;
      }

      function renderDistanceMatrix(entries) {
        const head = byId("comparison-distance-head");
        const body = byId("comparison-distance-body");
        head.replaceChildren();
        body.replaceChildren();
        const headRow = document.createElement("tr");
        const corner = document.createElement("th");
        corner.textContent = "Stacja";
        headRow.appendChild(corner);
        entries.forEach((entry, index) => {
          const cell = document.createElement("th");
          cell.textContent = `${index + 1}. ${stationDisplayName(entry.station)}`;
          headRow.appendChild(cell);
        });
        head.appendChild(headRow);
        entries.forEach((rowEntry, rowIndex) => {
          const row = document.createElement("tr");
          const heading = document.createElement("th");
          heading.textContent = `${rowIndex + 1}. ${stationDisplayName(rowEntry.station)}`;
          row.appendChild(heading);
          entries.forEach((columnEntry, columnIndex) => {
            const cell = document.createElement("td");
            cell.textContent = rowIndex === columnIndex
              ? "0 km"
              : `${haversineDistanceKm(rowEntry.station, columnEntry.station).toFixed(1)} km`;
            row.appendChild(cell);
          });
          body.appendChild(row);
        });
      }

      function renderStationComparison(entries, range) {
        const datatype = byId("comparison-datatype").value;
        entries.forEach((entry) => {
          updateVerifiedStationQuality(
            entry.station,
            { core_temperature_datatypes: entry.payload.observed_datatypes || [] },
            payloadCoreCompleteness(entry.payload),
          );
        });
        byId("comparison-empty").classList.add("d-none");
        byId("comparison-results").classList.remove("d-none");
        byId("comparison-common-range").textContent = `${range.commonStart}–${range.commonEnd}`;
        byId("comparison-active-range").textContent =
          `Analizowany okres: ${range.activeStart}–${range.activeEnd}`;
        byId("comparison-chart-title").textContent =
          `Porównanie miesięcznego ${datatype}${datatype === "TAVG" ? " NOAA" : ""}`;
        renderComparisonLegend(entries);
        renderComparisonTemperatureChart(entries, datatype);
        const rows = renderComparisonSummary(entries, datatype);
        renderDistanceMatrix(entries);

        const ranked = [...rows].sort((left, right) => {
          const scoreDifference = stationQuality(right.station).score - stationQuality(left.station).score;
          if (scoreDifference) return scoreDifference;
          return right.metrics.completeness - left.metrics.completeness;
        });
        const best = ranked[0];
        byId("comparison-best-station").textContent = stationDisplayName(best.station);
        byId("comparison-best-detail").textContent =
          `${stationQuality(best.station).score}/100 · kompletność ${best.metrics.completeness.toFixed(1)}%`;
        const averages = rows.map((entry) => entry.metrics.average).filter((value) => value !== null);
        byId("comparison-temperature-spread").textContent = averages.length
          ? `${(Math.max(...averages) - Math.min(...averages)).toFixed(2)}°C`
          : "brak danych";
        const lowestMissing = [...rows].sort((left, right) => left.metrics.missingDays - right.metrics.missingDays)[0];
        byId("comparison-lowest-missing").textContent = stationDisplayName(lowestMissing.station);
        byId("comparison-lowest-missing-detail").textContent =
          `${lowestMissing.metrics.missingDays} brakujących dni ${datatype}`;
        renderComparisonSelection();
      }

      async function loadStationComparison() {
        const stations = Array.from(comparisonStations.values());
        if (stations.length < 2) {
          clearComparisonResults();
          return;
        }
        const range = comparisonCommonRange();
        if (!range) {
          byId("comparison-status").textContent =
            "Wybrane stacje nie mają wspólnego okresu danych albo brakuje dat katalogowych.";
          clearComparisonResults("Brak wspólnego zakresu lat dla wybranych stacji.");
          return;
        }
        if (comparisonAbortController) comparisonAbortController.abort();
        const controller = new AbortController();
        comparisonAbortController = controller;
        byId("comparison-refresh-btn").disabled = true;
        byId("comparison-status").textContent =
          `Pobieranie wspólnego okresu ${range.activeStart}–${range.activeEnd} dla ${stations.length} stacji…`;
        const entries = [];
        const failures = [];
        try {
          // Dwie równoległe stacje nie wyczerpują limitu lokalnego serwera i puli tokenów NOAA.
          for (let index = 0; index < stations.length; index += 2) {
            const batch = stations.slice(index, index + 2);
            const results = await Promise.allSettled(
              batch.map((station) => fetchComparisonPayload(station, range, controller.signal)),
            );
            results.forEach((result, resultIndex) => {
              const station = batch[resultIndex];
              if (result.status === "fulfilled") entries.push({ station, payload: result.value });
              else if (result.reason?.name !== "AbortError") failures.push(stationDisplayName(station));
            });
          }
          if (controller.signal.aborted) return;
          if (entries.length < 2) {
            throw new Error("Mniej niż dwie stacje zwróciły porównywalne dane NOAA.");
          }
          lastComparisonPayloads = entries;
          renderStationComparison(entries, range);
          const datatype = byId("comparison-datatype").value;
          const missingDatatype = entries
            .filter((entry) => !(entry.payload.observed_datatypes || []).includes(datatype))
            .map((entry) => stationDisplayName(entry.station));
          byId("comparison-status").textContent =
            `Porównano ${entries.length} stacji dla wspólnego okresu ${range.activeStart}–${range.activeEnd}.` +
            (failures.length ? ` Bez danych: ${failures.join(", ")}.` : "") +
            (missingDatatype.length
              ? ` Brak ${datatype} w okresie porównania: ${missingDatatype.join(", ")}.`
              : "");
        } catch (error) {
          if (error?.name === "AbortError" || controller.signal.aborted) return;
          byId("comparison-status").textContent = `Nie udało się przygotować porównania: ${error}`;
          clearComparisonResults("Nie udało się pobrać wystarczających danych do porównania.");
        } finally {
          if (comparisonAbortController === controller) comparisonAbortController = null;
          byId("comparison-refresh-btn").disabled = comparisonStations.size < 2;
        }
      }

      async function selectBestStation() {
        if (!qualityFilteredResults.length) return;
        const button = byId("best-station-btn");
        button.disabled = true;
        button.textContent = "Sprawdzanie kandydatów…";
        const candidates = [...qualityFilteredResults]
          .sort((left, right) => stationQuality(right).score - stationQuality(left).score)
          .slice(0, 5);
        let checked = 0;
        for (const station of candidates) {
          try {
            const capabilities = await getStationCapabilities(station);
            updateVerifiedStationQuality(station, capabilities);
            checked += 1;
          } catch (error) {
            // A failed NOAA capability request does not discard a good catalogue candidate.
          }
        }
        applyQualityFilters();
        const best = candidates.sort((left, right) => {
          const leftQuality = stationQuality(left);
          const rightQuality = stationQuality(right);
          const scoreDifference = rightQuality.score - leftQuality.score;
          if (scoreDifference) return scoreDifference;
          return rightQuality.available_datatypes.length - leftQuality.available_datatypes.length;
        })[0];
        button.textContent = "Wybierz najlepszą stację";
        button.disabled = qualityFilteredResults.length === 0;
        if (best) {
          byId("message").textContent =
            `Najlepszy kandydat: ${best.name || best.city} — ${stationQuality(best).score}/100. ` +
            `Potwierdzono typy dla ${checked} z ${candidates.length} kandydatów.`;
          selectStation(best, { scrollToMap: true });
        }
      }

      function selectStation(station, { scrollToMap = false } = {}) {
        selectedStation = station;
        byId("selected-station").value = `${station.station_id} — ${station.name}`;
        byId("temperature-message").textContent = `Wybrano ${station.name} (${station.station_id}).`;
        renderResults(qualityFilteredResults);
        updateMapSelection();
        focusMapOnStation(station, { scrollToMap });
        byId("preview-empty").classList.add("d-none");
        byId("preview-content").classList.remove("d-none");
        renderPreviewQuality(station);
        renderNearbyQualityComparison(station);
        void checkTemperatureCapabilities(station);
        void loadTemperaturePreview(station);
      }

      async function runSearch() {
        setStatus("Loading", "warning");
        byId("message").textContent = "Wyszukiwanie...";

        const payload = {
          query: byId("query").value,
          country: normalizeCountryInput(byId("country").value),
          station_id: byId("station-id").value || null,
          sort: byId("sort").value,
          limit: maybeInt(byId("limit").value),
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
          stationCapabilitiesCache.clear();
          if (previewAbortController) previewAbortController.abort();
          selectedStation = null;
          stationTemperatureCapabilities = null;
          byId("selected-station").value = "";
          byId("temperature-json-btn").disabled = true;
          byId("temperature-capabilities").textContent =
            "Wybierz stację, aby sprawdzić dostępne typy danych NOAA.";
          byId("preview-content").classList.add("d-none");
          byId("preview-empty").classList.remove("d-none");
          byId("preview-refresh-btn").disabled = true;
          byId("preview-status").textContent =
            "Wybierz stację, aby zobaczyć temperatury i kompletność danych.";
          applyQualityFilters({ fitMap: true });
          void loadCountryBoundary(payload.country);

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
            if (source === "sample-default") {
              msg += " Wskazówka: wybierz kraj, aby pobrać pełny katalog stacji NOAA.";
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
        if (!qualityFilteredResults.length) return;
        const resp = await fetch("/api/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ format: fmt, rows: qualityFilteredResults }),
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

      async function downloadTemperatureJson() {
        if (!selectedStation) return;
        const mode = byId("temperature-export-mode").value;
        const startYear = maybeInt(byId("start-year").value);
        const endYear = maybeInt(byId("end-year").value);
        if (startYear === null || endYear === null || startYear > endYear) {
          byId("temperature-message").textContent = "Enter a valid start and end year.";
          return;
        }

        const button = byId("temperature-json-btn");
        button.disabled = true;
        button.textContent = "Downloading from NOAA...";
        const progressMessages = {
          heatmap: "Fetching TMAX/TMIN and building the unchanged Heatmapa matrix...",
          daily: "Fetching daily TMIN/TAVG/TMAX and calculating TAXN and amplitude...",
          monthly: "Building monthly TMIN/TAVG/TAXN/TMAX matrices and completeness...",
          extended: "Building extended monthly statistics, amplitude and completeness...",
        };
        byId("temperature-message").textContent = progressMessages[mode];
        try {
          const response = await fetch("/api/temperatures", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              station_id: selectedStation.station_id,
              start_year: startYear,
              end_year: endYear,
              mode,
            }),
          });
          const responseData = await response.json();
          if (!response.ok) {
            byId("temperature-message").textContent =
              `${responseData.code ?? "ERROR"}: ${responseData.message ?? "Temperature download failed"}`;
            return;
          }

          const payload = responseData.data;
          const blob = new Blob([JSON.stringify(payload, null, 4)], { type: "application/json;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          const fileStem = (selectedStation.city || selectedStation.name || payload.station_id)
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "_")
            .replace(/^_+|_+$/g, "") || payload.station_id.toLowerCase();
          const suffixes = {
            heatmap: "temperatures",
            daily: "daily_temperatures",
            monthly: "monthly_temperatures",
            extended: "monthly_statistics",
          };
          link.download = `${fileStem}_${suffixes[mode]}.json`;
          document.body.appendChild(link);
          link.click();
          link.remove();
          URL.revokeObjectURL(url);
          byId("temperature-message").textContent = mode === "heatmap"
            ? `Downloaded ${payload.years.length} year(s); missing: ${payload.final_missing_years.length}.`
            : `Downloaded ${payload.export_type} data for ${payload.period.start_date} — ${payload.period.end_date}.`;
        } catch (error) {
          byId("temperature-message").textContent = `Request failed: ${error}`;
        } finally {
          button.disabled = selectedStation === null || stationTemperatureCapabilities === null;
          button.textContent = "Download temperature JSON";
        }
      }

      byId("search-btn").addEventListener("click", runSearch);
      byId("export-json-btn").addEventListener("click", () => exportResults("json"));
      byId("export-csv-btn").addEventListener("click", () => exportResults("csv"));
      byId("temperature-json-btn").addEventListener("click", downloadTemperatureJson);
      byId("map-fit-btn").addEventListener("click", () => fitMapToStations(qualityFilteredResults));
      byId("map-reset-btn").addEventListener("click", () => resetMapView({ loadTiles: true }));
      byId("show-more-btn").addEventListener("click", () => {
        visibleResultCount += 250;
        renderResults(qualityFilteredResults);
      });
      for (const id of ["quality-min-years", "quality-min-coverage", "quality-grade-filter"]) {
        byId(id).addEventListener("change", () => applyQualityFilters({ fitMap: true }));
      }
      byId("quality-preset-btn").addEventListener("click", () => {
        byId("quality-min-years").value = "30";
        byId("quality-min-coverage").value = "90";
        byId("quality-grade-filter").value = "all";
        applyQualityFilters({ fitMap: true });
      });
      byId("quality-reset-btn").addEventListener("click", () => {
        byId("quality-min-years").value = "0";
        byId("quality-min-coverage").value = "0";
        byId("quality-grade-filter").value = "all";
        applyQualityFilters({ fitMap: true });
      });
      byId("best-station-btn").addEventListener("click", () => void selectBestStation());
      byId("preview-refresh-btn").addEventListener("click", () => {
        if (selectedStation) void loadTemperaturePreview(selectedStation);
      });
      byId("preview-years").addEventListener("change", () => {
        if (selectedStation) void loadTemperaturePreview(selectedStation);
      });
      byId("comparison-refresh-btn").addEventListener("click", () => void loadStationComparison());
      byId("comparison-years").addEventListener("change", () => {
        if (comparisonStations.size >= 2) void loadStationComparison();
      });
      byId("comparison-datatype").addEventListener("change", () => {
        if (comparisonStations.size >= 2) void loadStationComparison();
      });
      byId("comparison-clear-btn").addEventListener("click", () => {
        if (comparisonAbortController) comparisonAbortController.abort();
        comparisonStations.clear();
        renderComparisonSelection();
        byId("comparison-status").textContent = "Dodaj od 2 do 5 stacji przyciskiem „Porównaj”.";
        clearComparisonResults(
          "Wybierz co najmniej dwie stacje. Pierwsza dodana stacja będzie bazą dla różnic.",
        );
      });
      byId("end-year").value = String(new Date().getFullYear());
      initCountryAutocomplete();
      initializeStationMap();
      renderComparisonSelection();
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
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not hostname:
        raise ValueError("Remote URL must be a valid HTTPS URL")
    if hostname not in NOAA_ALLOWED_HOSTS:
        raise ValueError("Remote URL host must be an approved NOAA NCEI server")
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
    response_status = 0

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._run_request("GET", self._dispatch_get)

    def do_POST(self) -> None:
        self._run_request("POST", self._dispatch_post)

    def send_response(self, code: int, message: str | None = None) -> None:
        self.response_status = code
        super().send_response(code, message)

    def _run_request(self, method: str, dispatch: Callable[[], None]) -> None:
        self.request_id = uuid4().hex
        self.response_status = 0
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
            duration_seconds = time.monotonic() - started_at
            duration_ms = round(duration_seconds * 1000, 3)
            metric_path = path if path in _KNOWN_METRIC_PATHS else "<other>"
            _METRICS.record_request(method, metric_path, self.response_status or 499, duration_seconds)
            log_event("http_request_completed", method=method, path=path, duration_ms=duration_ms)
            reset_request_id(context_token)

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        static_asset = _STATIC_ASSETS.get(parsed.path)
        if static_asset is not None:
            asset_path, content_type = static_asset
            self._send_static_asset(asset_path, content_type=content_type)
            return
        if parsed.path == "/":
            csp_nonce = uuid4().hex
            token_configured = bool(TokenProvider.configured_tokens())
            env_file = resolve_env_file()
            using_private_file = env_file == private_env_file_path()
            token_alert_class = (
                "alert-success" if token_configured and using_private_file else "alert-warning"
            )
            if token_configured and using_private_file:
                configuration_notice = "configured in the private per-user .env file."
            elif token_configured:
                configuration_notice = (
                    "configured, but the legacy project .env is in use. Move it to the private "
                    "per-user configuration folder."
                )
            else:
                configuration_notice = (
                    "not configured. Add NOAA_API_TOKENS to the private per-user .env file; "
                    "GitHub Actions secrets are not automatically available to localhost."
                )
            page = (
                HTML_PAGE.replace("{{CSP_NONCE}}", csp_nonce)
                .replace("{{TOKEN_ALERT_CLASS}}", token_alert_class)
                .replace("{{TOKEN_STATUS_TEXT}}", configuration_notice)
                .replace(
                    "{{COUNTRY_OPTIONS_JSON}}",
                    json.dumps(sorted(COUNTRY_FIPS_CODES), ensure_ascii=False),
                )
            )
            self._send_text(
                page,
                content_type="text/html; charset=utf-8",
                csp_nonce=csp_nonce,
            )
            return
        if parsed.path in {"/health", "/health/live"}:
            self._send_json({"ok": True, "status": "alive"})
            return
        if parsed.path == "/health/ready":
            token_configured = bool(TokenProvider.configured_tokens())
            self._send_json(
                {
                    "ok": token_configured,
                    "status": "ready" if token_configured else "not_ready",
                    "checks": {
                        "http": "ok",
                        "cache": "optional",
                        "noaa_token": "configured" if token_configured else "missing",
                    },
                },
                status=HTTPStatus.OK if token_configured else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/openapi.json":
            self._send_json(OPENAPI_DOCUMENT, include_request_id=False)
            return
        if parsed.path == "/metrics":
            self._send_text(
                _METRICS.render_prometheus(),
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )
            return
        self._send_json({"code": "NOT_FOUND", "message": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _dispatch_post(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                content_type = self.headers.get_content_type().lower()
                if content_type != "application/json":
                    self._discard_request_body()
                    self._send_json(
                        {
                            "code": "UNSUPPORTED_MEDIA_TYPE",
                            "message": "Content-Type must be application/json",
                        },
                        status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    )
                    return
                if not self._request_origin_is_allowed():
                    self._discard_request_body()
                    self._send_json(
                        {"code": "FORBIDDEN_ORIGIN", "message": "Request Origin is not allowed"},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
            if parsed.path == "/api/search":
                self._handle_search()
                return
            if parsed.path == "/api/country-boundary":
                self._handle_country_boundary()
                return
            if parsed.path == "/api/temperature-capabilities":
                self._handle_temperature_capabilities()
                return
            if parsed.path == "/api/temperatures":
                self._handle_temperatures()
                return
            if parsed.path == "/api/export":
                self._handle_export()
                return
            length = _parse_content_length(self.headers.get("Content-Length"))
            if length > 0:
                self.rfile.read(length)
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

    def _discard_request_body(self) -> None:
        length = _parse_content_length(self.headers.get("Content-Length"))
        if length > 0:
            self.rfile.read(length)

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

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        include_request_id: bool = True,
    ) -> None:
        response_payload = {**payload, "request_id": self.request_id} if include_request_id else payload
        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)
        log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=int(status))

    def _send_text(
      self,
      payload: str,
      *,
      content_type: str,
      status: HTTPStatus = HTTPStatus.OK,
      csp_nonce: str | None = None,
    ) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self._send_security_headers(csp_nonce=csp_nonce)
        self.end_headers()
        self.wfile.write(body)
        log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=int(status))

    def _send_static_asset(self, asset_path: Path, *, content_type: str) -> None:
        try:
            body = asset_path.read_bytes()
        except OSError:
            self._send_json(
                {"code": "STATIC_ASSET_UNAVAILABLE", "message": "Static asset is unavailable"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self._send_security_headers(cache_control="public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)
        log_event("http_response_sent", method=self.command, path=urlparse(self.path).path, status=HTTPStatus.OK)

    def _send_security_headers(
      self,
      *,
      csp_nonce: str | None = None,
      cache_control: str = "no-store",
    ) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        self.send_header("Cache-Control", cache_control)
        if csp_nonce is None:
          policy = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        else:
          policy = (
            "default-src 'none'; "
            f"script-src 'nonce-{csp_nonce}' 'self'; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "img-src 'self' data: blob: https://tile.openstreetmap.org; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
          )
        self.send_header("Content-Security-Policy", policy)

    def _handle_search(self) -> None:
        payload = self._read_json()
        query = str(payload.get("query", "")).strip()

        if payload.get("remote_url") is not None or payload.get("cache_path") is not None:
            self._send_json(
                {
                    "code": "BAD_REQUEST",
                    "message": (
                        "remote_url and cache_path are not accepted by the GUI API; "
                        "select a country and use the managed NOAA cache"
                    ),
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return

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

        country = payload.get("country")
        station_id = payload.get("station_id")
        sort_by = str(payload.get("sort", "city"))

        if not _FETCH_LIMITER.acquire(timeout=FETCH_SLOT_TIMEOUT_SECONDS):
            _METRICS.record_server_busy()
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
            _METRICS.fetch_started()
            try:
                if country:
                    result = fetch_stations_for_country(
                        str(country),
                        timeout=REMOTE_REQUEST_DEADLINE_SECONDS,
                        cache_path=_country_cache_path(str(country)),
                        cache_ttl=cache_ttl,
                        refresh=bool(payload.get("refresh", False)),
                        stale_if_error=bool(payload.get("stale_if_error", True)),
                    )
                else:
                    result = fetch_stations_with_cache_details(
                        cache_path=None,
                        remote_url=None,
                        cache_ttl=cache_ttl,
                        refresh=bool(payload.get("refresh", False)),
                        allow_sample_fallback=bool(payload.get("allow_sample_fallback", False)),
                        stale_if_error=bool(payload.get("stale_if_error", False)),
                        max_stale_seconds=max_stale,
                        remote_timeout_seconds=REMOTE_REQUEST_DEADLINE_SECONDS,
                    )
            finally:
                _METRICS.fetch_finished()
                _FETCH_LIMITER.release()

            result_country = (
                result.metadata.get("country")
                if result.source.startswith("noaa-country")
                else country
            )
            normalized_country = str(result_country).strip() if result_country else None
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
            for row in rows:
                row["quality"] = station_quality_summary(row)
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except NoaaTimeoutError as exc:
            self._send_json(
                {"code": fetch_error_code(exc), "message": render_fetch_error(exc)},
                status=HTTPStatus.GATEWAY_TIMEOUT,
            )
            return
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
        if result.source in {"cache-stale", "sample-fallback"}:
            _METRICS.record_fallback(result.source)

    def _request_origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.username or parsed.password:
            return False
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        server_address = cast(tuple[str, int], self.server.server_address)
        server_port = int(server_address[1])
        return (parsed.port or 80) == server_port

    def _handle_country_boundary(self) -> None:
        payload = self._read_json()
        country = str(payload.get("country", "")).strip()
        if not country:
            self._send_json(
                {"code": "BAD_REQUEST", "message": "country is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            feature, source = fetch_country_boundary(country)
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except CountryBoundaryError:
            self._send_json(
                {
                    "code": "COUNTRY_BOUNDARY_UNAVAILABLE",
                    "message": "Country boundary is temporarily unavailable",
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_json({"data": feature, "source": source})

    def _handle_temperature_capabilities(self) -> None:
        payload = self._read_json()
        station_id = str(payload.get("station_id", "")).strip()
        if not station_id:
            self._send_json(
                {"code": "BAD_REQUEST", "message": "station_id is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if not _FETCH_LIMITER.acquire(timeout=FETCH_SLOT_TIMEOUT_SECONDS):
            _METRICS.record_server_busy()
            self._send_json(
                {"code": "SERVER_BUSY", "message": "Server is busy; retry shortly"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        try:
            _METRICS.fetch_started()
            result = fetch_station_temperature_capabilities(station_id)
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except NoaaTimeoutError as exc:
            self._send_json(
                {"code": fetch_error_code(exc), "message": render_fetch_error(exc)},
                status=HTTPStatus.GATEWAY_TIMEOUT,
            )
            return
        except NoaaClientError as exc:
            self._send_json(
                {"code": fetch_error_code(exc), "message": render_fetch_error(exc)},
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        finally:
            _METRICS.fetch_finished()
            _FETCH_LIMITER.release()

        self._send_json({"data": result})

    def _handle_temperatures(self) -> None:
        payload = self._read_json()
        station_id = str(payload.get("station_id", "")).strip()
        mode = str(payload.get("mode", "heatmap")).strip().lower()
        try:
            start_year = _parse_int(payload.get("start_year"), minimum=1763)
            end_year = _parse_int(payload.get("end_year"), minimum=1763)
            if not station_id:
                raise ValueError("station_id is required")
            if start_year is None or end_year is None:
                raise ValueError("start_year and end_year are required")
            if mode not in {"heatmap", "daily", "monthly", "extended"}:
                raise ValueError("mode must be heatmap, daily, monthly or extended")
        except (TypeError, ValueError) as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if not _FETCH_LIMITER.acquire(timeout=FETCH_SLOT_TIMEOUT_SECONDS):
            _METRICS.record_server_busy()
            self._send_json(
                {"code": "SERVER_BUSY", "message": "Server is busy; retry shortly"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        try:
            _METRICS.fetch_started()
            if mode == "heatmap":
                result = fetch_monthly_temperature_matrix(
                    station_id,
                    start_year,
                    end_year,
                    cache_dir=GUI_CACHE_DIR / "temperatures",
                )
            else:
                result = fetch_temperature_export(
                    station_id,
                    start_year,
                    end_year,
                    mode=mode,
                    cache_dir=GUI_CACHE_DIR / "temperatures",
                )
        except ValueError as exc:
            self._send_json(
                {"code": "BAD_REQUEST", "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except NoaaTimeoutError as exc:
            self._send_json(
                {"code": fetch_error_code(exc), "message": render_fetch_error(exc)},
                status=HTTPStatus.GATEWAY_TIMEOUT,
            )
            return
        except NoaaClientError as exc:
            self._send_json(
                {"code": fetch_error_code(exc), "message": render_fetch_error(exc)},
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        finally:
            _METRICS.fetch_finished()
            _FETCH_LIMITER.release()

        # The request envelope keeps correlation metadata in the API while the
        # browser downloads only `data`, which exactly matches Heatmapa's JSON.
        self._send_json({"data": result})

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
            self._send_security_headers()
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
            self._send_security_headers()
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
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow binding outside the local computer (unsafe without a reverse proxy)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        host_is_loopback = args.host.lower() == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        host_is_loopback = False
    if not host_is_loopback and not args.allow_network:
        parser.error("non-loopback --host requires explicit --allow-network")
    configure_logging(args.log_level)
    run_server(args.host, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
