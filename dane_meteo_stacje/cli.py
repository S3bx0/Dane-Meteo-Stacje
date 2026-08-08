from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Sequence

from .data import export_stations, fetch_stations_with_cache, load_stations, read_cache_metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wyszukiwarka stacji meteorologicznych NOAA")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Wyszukaj stację po nazwie miasta")
    search_parser.add_argument("query", help="Nazwa miasta lub części nazwy")
    search_parser.add_argument("--json", action="store_true", help="Wypisz wyniki w formacie JSON")
    search_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    search_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    search_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    search_parser.add_argument("--cache-ttl", type=int, default=3600, help="TTL cache w sekundach")
    search_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")
    search_parser.add_argument("--show-source", action="store_true", help="Pokaż źródło danych przy każdym wyniku")
    search_parser.add_argument("--limit", type=int, help="Maksymalna liczba wyników do wyświetlenia")
    search_parser.add_argument("--country", help="Filtruj wyniki po kraju")
    search_parser.add_argument("--station-id", help="Filtruj wyniki po ID stacji")
    search_parser.add_argument("--sort", choices=["city", "name", "station_id"], default="city", help="Sortowanie wyników")

    info_parser = subparsers.add_parser("info", help="Pokaż informacje o stacji")
    info_parser.add_argument("station_id", help="ID stacji NOAA")
    info_parser.add_argument("--json", action="store_true", help="Wypisz informacje w formacie JSON")
    info_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    info_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    info_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    info_parser.add_argument("--cache-ttl", type=int, default=3600, help="TTL cache w sekundach")
    info_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")

    export_parser = subparsers.add_parser("export", help="Eksportuj dane stacji do JSON/CSV")
    export_parser.add_argument("--output-json", help="Ścieżka do pliku JSON")
    export_parser.add_argument("--output-csv", help="Ścieżka do pliku CSV")
    export_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    export_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    export_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    export_parser.add_argument("--cache-ttl", type=int, default=3600, help="TTL cache w sekundach")
    export_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")
    export_parser.add_argument("--pretty", action="store_true", help="Zapisuj JSON w czytelnej, sformatowanej formie")
    export_parser.add_argument("--station-id", help="Eksportuj tylko stację o podanym ID")
    export_parser.add_argument("--noaa-like", action="store_true", help="Zapisuj dane w formacie zbliżonym do NOAA")

    cache_meta_parser = subparsers.add_parser("cache-meta", help="Pokaż metadane lokalnego cache")
    cache_meta_parser.add_argument("cache_path", help="Ścieżka do pliku cache")

    return parser


def search_stations(
    query: str,
    stations: Sequence[dict] | None = None,
    limit: int | None = None,
    country: str | None = None,
    station_id: str | None = None,
    sort_by: str = "city",
) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []

    station_list = list(stations or load_stations())
    matches = [
        station
        for station in station_list
        if (q in str(station["name"]).lower() or q in str(station["city"]).lower())
        and (country is None or str(station.get("country", "")).lower() == country.lower())
        and (station_id is None or str(station.get("station_id", "")).lower() == station_id.lower())
    ]

    if sort_by == "name":
        matches = sorted(matches, key=lambda station: str(station.get("name", "")).lower())
    elif sort_by == "station_id":
        matches = sorted(matches, key=lambda station: str(station.get("station_id", "")).lower())
    else:
        matches = sorted(matches, key=lambda station: str(station.get("city", "")).lower())

    if limit is None:
        return matches
    if limit < 0:
        return []
    return matches[:limit]


def print_search_results(
    results: Sequence[dict],
    as_json: bool = False,
    show_source: bool = False,
    cache_metadata: dict | None = None,
) -> None:
    if not results:
        if as_json:
            print(json.dumps([], ensure_ascii=False))
        else:
            print("Brak wyników.")
        return

    if as_json:
        print(json.dumps(list(results), ensure_ascii=False, indent=2))
        return

    for station in results:
        line = f"{station['city']} | {station['name']} | {station['station_id']}"
        if show_source:
            source_parts = [f"source: {station.get('source', 'unknown')}"]
            if cache_metadata:
                timestamp = cache_metadata.get("timestamp")
                if timestamp is not None:
                    source_parts.append(f"cache timestamp: {timestamp}")
            line += f" | {' | '.join(source_parts)}"
        print(line)


def print_station_info(station_id: str, as_json: bool = False, stations: Sequence[dict] | None = None) -> None:
    station = next((item for item in list(stations or load_stations()) if item["station_id"] == station_id), None)
    if station is None:
        if as_json:
            print(json.dumps({"station_id": station_id, "found": False}, ensure_ascii=False))
        else:
            print(f"Nie znaleziono stacji o ID {station_id}")
        return

    if as_json:
        print(json.dumps(station, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(station, indent=2, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "search":
        if getattr(args, "source", None):
            stations = load_stations(args.source)
            cache_metadata = None
        else:
            stations = fetch_stations_with_cache(
                getattr(args, "cache", None),
                getattr(args, "remote_url", None),
                cache_ttl=getattr(args, "cache_ttl", 3600),
                refresh=getattr(args, "refresh", False),
            )
            cache_metadata = read_cache_metadata(getattr(args, "cache", None)) if getattr(args, "cache", None) else None
        print_search_results(
            search_stations(
                args.query,
                stations=stations,
                limit=getattr(args, "limit", None),
                country=getattr(args, "country", None),
                station_id=getattr(args, "station_id", None),
                sort_by=getattr(args, "sort", "city"),
            ),
            as_json=args.json,
            show_source=getattr(args, "show_source", False),
            cache_metadata=cache_metadata,
        )
    elif args.command == "info":
        if getattr(args, "source", None):
            stations = load_stations(args.source)
        else:
            stations = fetch_stations_with_cache(
                getattr(args, "cache", None),
                getattr(args, "remote_url", None),
                cache_ttl=getattr(args, "cache_ttl", 3600),
                refresh=getattr(args, "refresh", False),
            )
        print_station_info(args.station_id, as_json=args.json, stations=stations)
    elif args.command == "export":
        stations = load_stations(args.source) if getattr(args, "source", None) else fetch_stations_with_cache(
            getattr(args, "cache", None),
            getattr(args, "remote_url", None),
            cache_ttl=getattr(args, "cache_ttl", 3600),
            refresh=getattr(args, "refresh", False),
        )
        station_id = getattr(args, "station_id", None)
        if station_id is not None:
            stations = [station for station in stations if str(station.get("station_id", "")).lower() == station_id.lower()]

        if getattr(args, "noaa_like", False):
            export_payload = {
                "metadata": {"resultset": {"count": len(stations)}},
                "results": [
                    {
                        "id": station.get("station_id"),
                        "name": station.get("name"),
                        "city": station.get("city"),
                        "country": station.get("country"),
                        "source": station.get("source", "local"),
                    }
                    for station in stations
                ],
            }
            if args.output_json is not None:
                Path(args.output_json).write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.output_csv is not None:
                with Path(args.output_csv).open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["id", "name", "city", "country", "source"])
                    writer.writeheader()
                    for station in stations:
                        writer.writerow(
                            {
                                "id": station.get("station_id"),
                                "name": station.get("name"),
                                "city": station.get("city"),
                                "country": station.get("country"),
                                "source": station.get("source", "local"),
                            }
                        )
        else:
            export_stations(
                stations,
                output_json=args.output_json,
                output_csv=args.output_csv,
                pretty=getattr(args, "pretty", False),
            )
    elif args.command == "cache-meta":
        metadata = read_cache_metadata(args.cache_path)
        if metadata:
            print(json.dumps(metadata, ensure_ascii=False, indent=2))
        else:
            print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
