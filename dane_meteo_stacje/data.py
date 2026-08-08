from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import requests

STATIONS = [
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


def _normalize_station(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    required_fields = {"station_id", "city", "name", "country"}
    if not required_fields.issubset(item.keys()):
        return None

    normalized = {
        "station_id": str(item["station_id"]),
        "city": str(item["city"]),
        "name": str(item["name"]),
        "country": str(item["country"]),
    }

    for key in ("source", "notes"):
        if key in item:
            normalized[key] = str(item[key])

    return normalized


def _normalize_noaa_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        results = payload.get("results")
        if not isinstance(results, list):
            return []
    elif isinstance(payload, list):
        results = payload
    else:
        return []

    normalized_records: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        station_id = item.get("id") or item.get("station_id")
        name = item.get("name") or item.get("station") or item.get("id")
        if not station_id or not name:
            continue

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
        normalized_records.append(
            {
                "station_id": str(station_id),
                "city": city_name,
                "name": str(name),
                "country": "USA",
                "source": "NOAA",
                "notes": "Mapped from NOAA station payload",
            }
        )
    return normalized_records


def load_stations(source: str | Path | None = None) -> list[dict[str, Any]]:
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

    normalized_records: list[dict[str, Any]] = []
    for item in records:
        normalized = _normalize_station(item)
        if normalized is not None:
            normalized_records.append(normalized)

    return normalized_records


def fetch_remote_stations(url: str, timeout: int = 10) -> list[dict[str, Any]]:
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except (requests.RequestException, requests.Timeout):
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("stations"), list):
        records = payload["stations"]
    else:
        records = _normalize_noaa_payload(payload)
        if records:
            return records
        return []

    normalized_records: list[dict[str, Any]] = []
    for item in records:
        normalized = _normalize_station(item)
        if normalized is not None:
            normalized_records.append(normalized)

    return normalized_records


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


def _write_cache_metadata(cache_path: str | Path, payload: list[dict[str, Any]]) -> None:
    metadata_path = _cache_metadata_path(cache_path)
    metadata_path.write_text(
        json.dumps({"timestamp": int(time.time()), "count": len(payload)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_stations_with_cache(
    cache_path: str | Path | None = None,
    remote_url: str | None = None,
    cache_ttl: int = 3600,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    if cache_path is not None:
        cache_file = Path(cache_path)
        if cache_file.exists() and not refresh:
            if cache_ttl < 0:
                cache_ttl = 0
            age = time.time() - cache_file.stat().st_mtime
            if age <= cache_ttl:
                cached = load_stations(cache_file)
                if cached:
                    return cached

    remote = remote_url or os.getenv("NOAA_STATIONS_URL")
    if remote:
        fetched = fetch_remote_stations(remote)
        if fetched:
            if cache_path is not None:
                Path(cache_path).write_text(json.dumps(fetched, ensure_ascii=False, indent=2), encoding="utf-8")
                _write_cache_metadata(cache_path, fetched)
            return fetched

    return [dict(station) for station in STATIONS]


def export_stations(
    stations: Sequence[dict[str, Any]],
    output_json: str | Path | None = None,
    output_csv: str | Path | None = None,
    pretty: bool = False,
) -> None:
    if output_json is not None:
        payload = list(stations)
        if pretty:
            Path(output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            Path(output_json).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if output_csv is not None:
        with Path(output_csv).open("w", encoding="utf-8", newline="") as handle:
            rows = list(stations)
            if rows:
                fieldnames = sorted({key for row in rows for key in row.keys()})
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
