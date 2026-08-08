from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
