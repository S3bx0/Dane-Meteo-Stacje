from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .data import (
    NoaaAuthError,
    NoaaClientError,
    NoaaNetworkError,
    NoaaPayloadError,
    NoaaRateLimitError,
    StationRecord,
    export_stations,
    fetch_stations_with_cache_details,
    load_stations,
    read_cache_metadata,
)


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Wartość musi być >= 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Wartość musi być > 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wyszukiwarka stacji meteorologicznych NOAA")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Wyszukaj stację po nazwie miasta")
    search_parser.add_argument("query", help="Nazwa miasta lub części nazwy")
    search_parser.add_argument("--json", action="store_true", help="Wypisz wyniki w formacie JSON")
    search_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    search_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    search_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    search_parser.add_argument("--cache-ttl", type=_non_negative_int, default=3600, help="TTL cache w sekundach")
    search_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")
    search_parser.add_argument("--show-source", action="store_true", help="Pokaż źródło danych przy każdym wyniku")
    search_parser.add_argument("--limit", type=_positive_int, help="Maksymalna liczba wyników do wyświetlenia")
    search_parser.add_argument("--country", help="Filtruj wyniki po kraju")
    search_parser.add_argument("--station-id", help="Filtruj wyniki po ID stacji")
    search_parser.add_argument(
        "--sort",
        choices=["city", "name", "station_id"],
        default="city",
        help="Sortowanie wyników",
    )
    search_parser.add_argument(
        "--allow-sample-fallback",
        action="store_true",
        help="W razie błędu NOAA użyj danych przykładowych zamiast błędu",
    )
    search_parser.add_argument(
        "--stale-if-error",
        action="store_true",
        help="W razie błędu NOAA użyj przeterminowanego cache, jeśli istnieje",
    )
    search_parser.add_argument(
        "--max-stale",
        type=_non_negative_int,
        help="Maksymalny wiek przeterminowanego cache (sekundy) dla --stale-if-error",
    )
    search_parser.add_argument("--verbose", action="store_true", help="Pokaż dodatkowe informacje diagnostyczne")

    info_parser = subparsers.add_parser("info", help="Pokaż informacje o stacji")
    info_parser.add_argument("station_id", help="ID stacji NOAA")
    info_parser.add_argument("--json", action="store_true", help="Wypisz informacje w formacie JSON")
    info_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    info_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    info_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    info_parser.add_argument("--cache-ttl", type=_non_negative_int, default=3600, help="TTL cache w sekundach")
    info_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")
    info_parser.add_argument(
        "--allow-sample-fallback",
        action="store_true",
        help="W razie błędu NOAA użyj danych przykładowych zamiast błędu",
    )
    info_parser.add_argument(
        "--stale-if-error",
        action="store_true",
        help="W razie błędu NOAA użyj przeterminowanego cache, jeśli istnieje",
    )
    info_parser.add_argument(
        "--max-stale",
        type=_non_negative_int,
        help="Maksymalny wiek przeterminowanego cache (sekundy) dla --stale-if-error",
    )
    info_parser.add_argument("--verbose", action="store_true", help="Pokaż dodatkowe informacje diagnostyczne")

    export_parser = subparsers.add_parser("export", help="Eksportuj dane stacji do JSON/CSV")
    export_parser.add_argument("--output-json", help="Ścieżka do pliku JSON")
    export_parser.add_argument("--output-csv", help="Ścieżka do pliku CSV")
    export_parser.add_argument("--source", help="Ścieżka do pliku JSON ze stacjami")
    export_parser.add_argument("--cache", help="Ścieżka do lokalnego cache JSON")
    export_parser.add_argument("--remote-url", help="URL z danymi stacji w formacie JSON")
    export_parser.add_argument("--cache-ttl", type=_non_negative_int, default=3600, help="TTL cache w sekundach")
    export_parser.add_argument("--refresh", action="store_true", help="Wymuś odświeżenie danych z zewnętrznego źródła")
    export_parser.add_argument("--pretty", action="store_true", help="Zapisuj JSON w czytelnej, sformatowanej formie")
    export_parser.add_argument("--station-id", help="Eksportuj tylko stację o podanym ID")
    export_parser.add_argument("--noaa-like", action="store_true", help="Zapisuj dane w formacie zbliżonym do NOAA")
    export_parser.add_argument(
        "--allow-sample-fallback",
        action="store_true",
        help="W razie błędu NOAA użyj danych przykładowych zamiast błędu",
    )
    export_parser.add_argument(
        "--stale-if-error",
        action="store_true",
        help="W razie błędu NOAA użyj przeterminowanego cache, jeśli istnieje",
    )
    export_parser.add_argument(
        "--max-stale",
        type=_non_negative_int,
        help="Maksymalny wiek przeterminowanego cache (sekundy) dla --stale-if-error",
    )
    export_parser.add_argument("--verbose", action="store_true", help="Pokaż dodatkowe informacje diagnostyczne")

    cache_meta_parser = subparsers.add_parser("cache-meta", help="Pokaż metadane lokalnego cache")
    cache_meta_parser.add_argument("cache_path", help="Ścieżka do pliku cache")

    return parser


def search_stations(
    query: str,
    stations: Sequence[StationRecord] | None = None,
    limit: int | None = None,
    country: str | None = None,
    station_id: str | None = None,
    sort_by: str = "city",
) -> list[StationRecord]:
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
    results: Sequence[StationRecord],
    as_json: bool = False,
    show_source: bool = False,
    cache_metadata: dict[str, Any] | None = None,
    fetch_source: str | None = None,
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
            if fetch_source:
                source_parts.append(f"fetch source: {fetch_source}")
            if cache_metadata:
                timestamp = cache_metadata.get("timestamp")
                if timestamp is not None:
                    source_parts.append(f"cache timestamp: {timestamp}")
            line += f" | {' | '.join(source_parts)}"
        print(line)


def _load_stations_from_runtime(args: argparse.Namespace) -> tuple[list[StationRecord], str, dict[str, Any]]:
    if getattr(args, "source", None):
        return load_stations(args.source), "source-file", {}

    result = fetch_stations_with_cache_details(
        cache_path=getattr(args, "cache", None),
        remote_url=getattr(args, "remote_url", None),
        cache_ttl=getattr(args, "cache_ttl", 3600),
        refresh=getattr(args, "refresh", False),
        allow_sample_fallback=getattr(args, "allow_sample_fallback", False),
        stale_if_error=getattr(args, "stale_if_error", False),
        max_stale_seconds=getattr(args, "max_stale", None),
    )
    return result.stations, result.source, result.metadata


def _filter_stations_for_export(stations: list[StationRecord], station_id: str | None) -> list[StationRecord]:
    if station_id is None:
        return stations
    return [station for station in stations if str(station.get("station_id", "")).lower() == station_id.lower()]


def _render_fetch_error(exc: NoaaClientError) -> str:
    if isinstance(exc, NoaaAuthError):
        return "NOAA auth error: sprawdź token (NOAA_TOKEN) lub uprawnienia."
    if isinstance(exc, NoaaRateLimitError):
        return "NOAA rate limit: przekroczono limit zapytań (HTTP 429)."
    if isinstance(exc, NoaaPayloadError):
        return "NOAA payload error: nieobsługiwany format odpowiedzi NOAA."
    if isinstance(exc, NoaaNetworkError):
        return "NOAA network error: nie udało się pobrać danych z NOAA."
    return "NOAA error: nie udało się pobrać danych."


def _fetch_error_code(exc: NoaaClientError) -> str:
    if isinstance(exc, NoaaAuthError):
        return "NOAA_AUTH"
    if isinstance(exc, NoaaRateLimitError):
        return "NOAA_RATE_LIMIT"
    if isinstance(exc, NoaaPayloadError):
        return "NOAA_PAYLOAD"
    if isinstance(exc, NoaaNetworkError):
        return "NOAA_NETWORK"
    return "NOAA_UNKNOWN"


def _warning_code(fetch_source: str, fetch_metadata: dict[str, Any]) -> str:
    if fetch_source == "sample-fallback":
        return "FALLBACK_SAMPLE"
    if fetch_source == "cache-stale":
        return "FALLBACK_STALE_CACHE"
    if fetch_source == "sample-default":
        return "SOURCE_SAMPLE_DEFAULT"
    if fetch_metadata.get("warning"):
        return "FETCH_WARNING"
    return "INFO"


def _emit_warning(fetch_source: str, fetch_metadata: dict[str, Any], verbose: bool) -> None:
    warning = fetch_metadata.get("warning")
    if not warning:
        return

    code = _warning_code(fetch_source, fetch_metadata)
    print(f"[warning][{code}] {warning}", file=sys.stderr)
    if verbose:
        debug_payload = {
            "fetch_source": fetch_source,
            "fetch_metadata": fetch_metadata,
        }
        print(f"[debug] {json.dumps(debug_payload, ensure_ascii=False, sort_keys=True)}", file=sys.stderr)


def print_station_info(station_id: str, as_json: bool = False, stations: Sequence[StationRecord] | None = None) -> None:
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
    verbose = bool(getattr(args, "verbose", False))
    try:
        if args.command == "search":
            stations, fetch_source, fetch_metadata = _load_stations_from_runtime(args)
            cache_path = getattr(args, "cache", None)
            cache_metadata = read_cache_metadata(cache_path) if isinstance(cache_path, str) else None
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
                fetch_source=fetch_source,
            )
            _emit_warning(fetch_source, fetch_metadata, verbose)
        elif args.command == "info":
            stations, fetch_source, fetch_metadata = _load_stations_from_runtime(args)
            print_station_info(args.station_id, as_json=args.json, stations=stations)
            _emit_warning(fetch_source, fetch_metadata, verbose)
        elif args.command == "export":
            stations, fetch_source, fetch_metadata = _load_stations_from_runtime(args)
            stations = _filter_stations_for_export(stations, getattr(args, "station_id", None))
            export_stations(
                stations,
                output_json=args.output_json,
                output_csv=args.output_csv,
                pretty=getattr(args, "pretty", False),
                noaa_like=getattr(args, "noaa_like", False),
            )
            _emit_warning(fetch_source, fetch_metadata, verbose)
        elif args.command == "cache-meta":
            metadata = read_cache_metadata(args.cache_path)
            if metadata:
                print(json.dumps(metadata, ensure_ascii=False, indent=2))
            else:
                print("{}")
        return 0
    except NoaaClientError as exc:
        code = _fetch_error_code(exc)
        print(f"[error][{code}] {_render_fetch_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
