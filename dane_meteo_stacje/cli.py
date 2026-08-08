from __future__ import annotations

import argparse
import json
from typing import Sequence

from .data import load_stations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wyszukiwarka stacji meteorologicznych NOAA")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Wyszukaj stację po nazwie miasta")
    search_parser.add_argument("query", help="Nazwa miasta lub części nazwy")
    search_parser.add_argument("--json", action="store_true", help="Wypisz wyniki w formacie JSON")
    search_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")

    info_parser = subparsers.add_parser("info", help="Pokaż informacje o stacji")
    info_parser.add_argument("station_id", help="ID stacji NOAA")
    info_parser.add_argument("--json", action="store_true", help="Wypisz informacje w formacie JSON")
    info_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")

    return parser


def search_stations(query: str, stations: Sequence[dict] | None = None) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []

    station_list = list(stations or load_stations())
    return [
        station
        for station in station_list
        if q in str(station["name"]).lower() or q in str(station["city"]).lower()
    ]


def print_search_results(results: Sequence[dict], as_json: bool = False) -> None:
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
        print(f"{station['city']} | {station['name']} | {station['station_id']}")


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
        stations = load_stations(args.source) if getattr(args, "source", None) else None
        print_search_results(search_stations(args.query, stations=stations), as_json=args.json)
    elif args.command == "info":
        stations = load_stations(args.source) if getattr(args, "source", None) else None
        print_station_info(args.station_id, as_json=args.json, stations=stations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
