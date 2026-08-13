from __future__ import annotations

import argparse
import csv
import io
import ipaddress
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

from .api_contract import OPENAPI_DOCUMENT
from .cli import search_stations
from .countries import COUNTRY_FIPS_CODES, country_to_fips_code
from .data import (
    NoaaClientError,
    NoaaTimeoutError,
    StationRecord,
    TokenProvider,
    fetch_monthly_temperature_matrix,
    fetch_station_temperature_capabilities,
    fetch_stations_for_country,
    fetch_stations_with_cache_details,
    fetch_temperature_export,
    private_env_file_path,
    resolve_env_file,
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
GUI_CACHE_DIR = private_env_file_path().parent / "cache"
_FETCH_LIMITER = BoundedSemaphore(MAX_CONCURRENT_FETCHES)
_METRICS = MetricsRegistry()
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


def _country_cache_path(country: str) -> Path:
  return GUI_CACHE_DIR / "stations" / f"{country_to_fips_code(country)}.json"


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
        background: #0d6efd;
        box-shadow: 0 1px 5px rgba(15, 23, 42, 0.55);
      }
      .station-map-dot:hover span, .station-map-dot:focus span { background: #084298; }
      .station-map-dot.selected span {
        width: 20px;
        height: 20px;
        margin: -2px;
        background: #dc3545;
        box-shadow: 0 0 0 3px rgba(220, 53, 69, 0.24), 0 2px 7px rgba(15, 23, 42, 0.6);
      }
      .station-map-cluster span {
        display: grid;
        width: 38px;
        height: 38px;
        place-items: center;
        border: 3px solid rgba(255, 255, 255, 0.9);
        border-radius: 50%;
        background: rgba(13, 110, 253, 0.88);
        color: #ffffff;
        font-weight: 700;
        box-shadow: 0 2px 8px rgba(15, 23, 42, 0.42);
      }
      .station-map-cluster.medium span { width: 44px; height: 44px; background: rgba(10, 88, 202, 0.9); }
      .station-map-cluster.large span { width: 50px; height: 50px; background: rgba(8, 66, 152, 0.92); }
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
      .map-legend-dot.selected { background: #dc3545; }
      @media (max-width: 767.98px) {
        .station-map-frame { height: 24rem; min-height: 24rem; }
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

          <div class="col-md-7">
            <label for="remote-url" class="form-label">Advanced Remote URL (optional)</label>
            <input
              id="remote-url"
              class="form-control mono"
              placeholder="https://www.ncei.noaa.gov/cdo-web/api/v2/stations?..."
            />
            <div class="form-text">
              Leave empty: Country automatically selects and paginates the NOAA GHCND station catalogue.
            </div>
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

      <section class="panel rounded-4 shadow-sm p-3 p-md-4 mb-3">
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
                <th>From</th>
                <th>To</th>
                <th>Coverage</th>
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
              <span class="map-legend-dot"></span> Wynik wyszukiwania
            </span>
            <span class="d-inline-flex align-items-center gap-1">
              <span class="map-legend-dot selected"></span> Wybrana stacja
            </span>
          </div>
        </div>
        <div id="map-selected-station" class="small fw-semibold mt-1" aria-live="polite">
          Nie wybrano stacji.
        </div>
        <div class="small text-secondary mt-1">
          Podkład © OpenStreetMap contributors. Do wyświetlania szczegółów mapy wymagane jest połączenie z internetem.
        </div>
      </section>

      <section class="panel rounded-4 shadow-sm p-3 p-md-4">
        <h2 class="h5">NOAA temperature JSON</h2>
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
      let selectedStation = null;
      let stationTemperatureCapabilities = null;
      let visibleResultCount = 250;
      let stationMap = null;
      let stationTileLayer = null;
      let stationMarkerLayer = null;
      let mapTileErrorShown = false;
      const stationMarkersById = new Map();

      const SUPPORTED_COUNTRY_OPTIONS = {{COUNTRY_OPTIONS_JSON}};
      const WORLD_MAP_CENTER = [20, 0];
      const WORLD_MAP_ZOOM = 2;

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

      function stationIcon(isSelected = false) {
        return L.divIcon({
          className: `station-map-dot${isSelected ? " selected" : ""}`,
          html: '<span aria-hidden="true"></span>',
          iconSize: [18, 18],
          iconAnchor: [9, 9],
          popupAnchor: [0, -9],
        });
      }

      function stationClusterIcon(cluster) {
        const count = cluster.getChildCount();
        const sizeClass = count >= 100 ? "large" : count >= 20 ? "medium" : "small";
        const diameter = count >= 100 ? 50 : count >= 20 ? 44 : 38;
        return L.divIcon({
          className: `station-map-cluster ${sizeClass}`,
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
        const selectButton = document.createElement("button");
        selectButton.type = "button";
        selectButton.className = "btn btn-sm btn-primary w-100";
        selectButton.textContent = "Wybierz tę stację";
        selectButton.addEventListener("click", () => selectStation(station));
        content.append(title, meta, selectButton);
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
          marker.setIcon(stationIcon(Boolean(isSelected)));
          if (marker.getElement()) marker.getElement().setAttribute("aria-pressed", isSelected ? "true" : "false");
        }
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
          const marker = L.marker([coordinates.latitude, coordinates.longitude], {
            icon: stationIcon(
              Boolean(selectedStation && String(station.station_id) === String(selectedStation.station_id)),
            ),
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
            [row.datacoverage, ""],
          ]) {
            const cell = document.createElement("td");
            cell.textContent = value ?? "";
            if (className) cell.className = className;
            tr.appendChild(cell);
          }
          const actionCell = document.createElement("td");
          const selectButton = document.createElement("button");
          selectButton.type = "button";
          selectButton.className = isSelected ? "btn btn-sm btn-primary" : "btn btn-sm btn-outline-primary";
          selectButton.textContent = "Select";
          selectButton.addEventListener("click", () => selectStation(row, { scrollToMap: true }));
          actionCell.appendChild(selectButton);
          tr.appendChild(actionCell);
          body.appendChild(tr);
        }
        const pagination = byId("result-pagination");
        const hasMore = visibleRows.length < rows.length;
        pagination.classList.toggle("d-none", rows.length <= 250);
        pagination.classList.toggle("d-flex", rows.length > 250);
        byId("result-pagination-text").textContent = `Showing ${visibleRows.length} of ${rows.length}.`;
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

      async function checkTemperatureCapabilities(station) {
        stationTemperatureCapabilities = null;
        byId("temperature-json-btn").disabled = true;
        byId("temperature-capabilities").textContent = "Sprawdzanie typów danych dla wybranej stacji...";
        try {
          const response = await fetch("/api/temperature-capabilities", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ station_id: station.station_id }),
          });
          const responseData = await response.json();
          if (!response.ok) {
            byId("temperature-capabilities").textContent =
              `${responseData.code ?? "ERROR"}: ${responseData.message ?? "Nie udało się sprawdzić typów danych"}`;
            return;
          }
          stationTemperatureCapabilities = responseData.data;
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
          byId("temperature-capabilities").textContent = `Nie udało się sprawdzić typów danych: ${error}`;
        }
      }

      function selectStation(station, { scrollToMap = false } = {}) {
        selectedStation = station;
        byId("selected-station").value = `${station.station_id} — ${station.name}`;
        byId("temperature-message").textContent = `Selected ${station.name} (${station.station_id}).`;
        renderResults(lastResults);
        updateMapSelection();
        focusMapOnStation(station, { scrollToMap });
        void checkTemperatureCapabilities(station);
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
          visibleResultCount = 250;
          renderResults(lastResults);
          renderStationMap(lastResults);
          if (!fitMapToStations(lastResults)) resetMapView();
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
      byId("map-fit-btn").addEventListener("click", () => fitMapToStations(lastResults));
      byId("map-reset-btn").addEventListener("click", () => resetMapView({ loadTiles: true }));
      byId("show-more-btn").addEventListener("click", () => {
        visibleResultCount += 250;
        renderResults(lastResults);
      });
      byId("end-year").value = String(new Date().getFullYear());
      initCountryAutocomplete();
      initializeStationMap();
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
                if cache_path is not None:
                    GUI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                if country and not remote_url:
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
                        cache_path=cache_path,
                        remote_url=str(remote_url) if remote_url else None,
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
