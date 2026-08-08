import argparse
import difflib
import io
import time
import unicodedata
from typing import Optional

import pandas as pd
import requests

STATIONS_URLS = [
    "https://www1.ncdc.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt",
    "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt",
    "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/doc/ghcnd-stations.txt",
]
COUNTRIES_URLS = [
    "https://www1.ncdc.noaa.gov/pub/data/ghcn/daily/ghcnd-countries.txt",
    "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-countries.txt",
]
STATION_WIDTHS = [11, 9, 10, 7, 3, 31, 4, 4]
STATION_COLUMNS = ["ID", "Latitude", "Longitude", "Elevation", "State", "Name", "GSN", "HCN"]
DEFAULT_MAX_RESULTS = 30
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_RETRY_COUNT = 3

ISO_TO_GHCN_CODE = {
    "DE": "GM",
    "AT": "AU",
    "CZ": "EZ",
    "DK": "DA",
    "GR": "GR",
    "ES": "SP",
    "PT": "PO",
    "RO": "RO",
    "SI": "SI",
    "SK": "LO",
    "SE": "SW",
    "NO": "NO",
    "FI": "FI",
    "GB": "UK",
    "IE": "EI",
    "CH": "SZ",
    "HR": "HR",
    "HU": "HU",
    "US": "US",
    "PL": "PL",
}


def load_stations() -> Optional[pd.DataFrame]:
    stations_text = fetch_text_from_first_available_url(STATIONS_URLS)
    if stations_text is None:
        print("Błąd pobierania listy stacji po kilku próbach.")
        return None

    try:
        stations = pd.read_fwf(io.StringIO(stations_text), widths=STATION_WIDTHS, names=STATION_COLUMNS)
    except Exception as exc:
        print(f"Błąd pobierania listy stacji: {exc}")
        return None

    stations["ID"] = stations["ID"].astype(str).str.strip()
    stations["Name"] = stations["Name"].astype(str).str.strip()
    stations = stations.dropna(subset=["ID", "Name"]).reset_index(drop=True)
    stations["NameNorm"] = stations["Name"].map(normalize_text)
    return stations


def load_countries() -> Optional[pd.DataFrame]:
    countries_text = fetch_text_from_first_available_url(COUNTRIES_URLS)
    if countries_text is None:
        return None

    try:
        countries = pd.read_fwf(io.StringIO(countries_text), widths=[2, 1, 60], names=["Code", "_sep", "Country"])
    except Exception:
        return None

    countries["Code"] = countries["Code"].astype(str).str.strip().str.upper()
    countries["Country"] = countries["Country"].astype(str).str.strip()
    countries = countries[(countries["Code"] != "") & (countries["Country"] != "")].copy()
    countries["CountryNorm"] = countries["Country"].map(normalize_text)
    return countries[["Code", "Country", "CountryNorm"]]


def normalize_text(value: str) -> str:
    value = str(value).strip().lower()
    value = "".join(
        character for character in unicodedata.normalize("NFKD", value) if not unicodedata.combining(character)
    )
    return " ".join(value.split())


def fetch_text_with_retries(
    url: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
) -> Optional[str]:
    last_error: Optional[Exception] = None

    for attempt in range(1, retry_count + 1):
        try:
            response = requests.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retry_count:
                time.sleep(0.8 * attempt)

    if last_error:
        print(f"Błąd połączenia dla {url}: {last_error}")
    return None


def fetch_text_from_first_available_url(
    urls: list[str],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
) -> Optional[str]:
    for url in urls:
        payload = fetch_text_with_retries(url, timeout_seconds=timeout_seconds, retry_count=retry_count)
        if payload is not None:
            return payload
    return None


def city_match_score(query_norm: str, station_name_norm: str) -> float:
    if not station_name_norm:
        return 0.0

    base_ratio = difflib.SequenceMatcher(None, query_norm, station_name_norm).ratio()
    token_ratios = [difflib.SequenceMatcher(None, query_norm, token).ratio() for token in station_name_norm.split()]
    best_token_ratio = max(token_ratios, default=0.0)

    score = max(base_ratio, best_token_ratio)

    if query_norm in station_name_norm:
        score += 0.35
    if station_name_norm.startswith(query_norm):
        score += 0.20
    if any(token.startswith(query_norm) for token in station_name_norm.split()):
        score += 0.10

    return score


def resolve_country_codes(country_query: str, countries: Optional[pd.DataFrame]) -> list[str]:
    query = country_query.strip()
    query_upper = query.upper()
    query_norm = normalize_text(query)

    if not query:
        return []

    codes: list[str] = []
    if len(query_upper) == 2:
        codes.append(query_upper)
        if query_upper in ISO_TO_GHCN_CODE:
            mapped = ISO_TO_GHCN_CODE[query_upper]
            if mapped not in codes:
                codes.append(mapped)

    if countries is not None:
        code_set = set(countries["Code"].values)
        if len(query_upper) == 2 and query_upper in code_set and query_upper not in codes:
            codes.append(query_upper)

        by_name = countries[countries["CountryNorm"].str.contains(query_norm, na=False)]
        for code in by_name["Code"].tolist():
            if code not in codes:
                codes.append(code)

    return codes


def search_by_country(stations: pd.DataFrame, country_query: str, countries: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    codes = resolve_country_codes(country_query, countries)
    if not codes:
        print("Nie rozpoznano kraju. Użyj kodu (np. PL/DE) albo nazwy kraju.")
        return pd.DataFrame(columns=stations.columns)

    results = stations[stations["ID"].str[:2].isin(codes)].copy()
    if results.empty and len(country_query.strip()) == 2 and country_query.strip().upper() in ISO_TO_GHCN_CODE:
        mapped = ISO_TO_GHCN_CODE[country_query.strip().upper()]
        print(f"Brak stacji dla kodu {country_query.strip().upper()}. Spróbowano też mapowania na kod GHCN: {mapped}.")

    return results.sort_values(by=["Name", "ID"])


def split_country_city_query(query: str) -> tuple[Optional[str], str]:
    text = query.strip()
    if "," not in text:
        return None, text

    country_part, city_part = text.split(",", 1)
    country_part = country_part.strip()
    city_part = city_part.strip()

    if not country_part or not city_part:
        return None, text

    return country_part, city_part


def search_by_city(stations: pd.DataFrame, city_name: str) -> pd.DataFrame:
    query_norm = normalize_text(city_name)
    if not query_norm:
        return pd.DataFrame(columns=stations.columns)

    stations = stations.copy()
    if "NameNorm" not in stations.columns:
        stations["NameNorm"] = stations["Name"].map(normalize_text)

    exact_matches = stations[stations["NameNorm"].str.contains(query_norm, na=False)].copy()
    if not exact_matches.empty:
        exact_matches["_score"] = exact_matches["NameNorm"].map(lambda name: city_match_score(query_norm, name))
        exact_matches = exact_matches.sort_values(by=["_score", "Name", "ID"], ascending=[False, True, True])
        return exact_matches.drop(columns=["_score", "NameNorm"], errors="ignore")

    fuzzy_matches = stations.copy()
    fuzzy_matches["_score"] = fuzzy_matches["NameNorm"].map(lambda name: city_match_score(query_norm, name))
    fuzzy_matches = fuzzy_matches[fuzzy_matches["_score"] >= 0.55]
    fuzzy_matches = fuzzy_matches.sort_values(by=["_score", "Name", "ID"], ascending=[False, True, True])

    return fuzzy_matches.drop(columns=["_score", "NameNorm"], errors="ignore")


def search_by_country_and_city(
    stations: pd.DataFrame,
    countries: Optional[pd.DataFrame],
    country_query: str,
    city_query: str,
) -> pd.DataFrame:
    by_country = search_by_country(stations, country_query, countries=countries)
    if by_country.empty:
        return by_country
    return search_by_city(by_country, city_query)


def display_results(results: pd.DataFrame, max_results: int) -> None:
    total = len(results)
    shown = min(total, max_results)

    print(f"\nZnaleziono {total} stacji. Pokazuję {shown} pierwszych:\n")
    for index, row in enumerate(results.head(max_results).itertuples(index=False), start=1):
        print(
            f"{index:>3}. ID: {row.ID:<11} | Nazwa: {row.Name:<31} | "
            f"Lat: {row.Latitude:>8} | Lon: {row.Longitude:>9}"
        )

    if total > max_results:
        print("\nWyników jest więcej. Doprecyzuj zapytanie, aby zawęzić listę.")


def choose_station_id(results: pd.DataFrame, max_results: int, auto_select_top: bool = False) -> Optional[str]:
    if results.empty:
        print("Brak wyników.")
        return None

    if len(results) == 1:
        only_id = results.iloc[0]["ID"]
        print(f"Znaleziono dokładnie jedną stację: {only_id}")
        return only_id

    if auto_select_top:
        top_id = results.iloc[0]["ID"]
        print(f"Tryb automatyczny: wybrano najwyżej oceniony wynik: {top_id}")
        return top_id

    display_results(results, max_results)
    visible_results = results.head(max_results).reset_index(drop=True)

    while True:
        choice = input(
            "\nWpisz numer z listy, pełne ID stacji, 'pomin' aby szukać dalej, albo 'koniec': "
        ).strip()

        lowered = choice.lower()
        if lowered in {"pomin", "pomij", "pomiń"}:
            return None
        if lowered == "koniec":
            return ""

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(visible_results):
                return visible_results.iloc[index - 1]["ID"]
            print("Nieprawidłowy numer z listy.")
            continue

        normalized_id = choice.upper()
        if normalized_id in visible_results["ID"].values:
            return normalized_id

        print("Nie znaleziono takiego ID w pokazanej liście.")


def interactive_station_picker(
    stations: pd.DataFrame,
    countries: Optional[pd.DataFrame],
    max_results: int = DEFAULT_MAX_RESULTS,
) -> Optional[str]:
    while True:
        mode = input("\nSzukaj po mieście (M), kraju (K), 'koniec' aby wyjść: ").strip().lower()

        if mode == "koniec":
            return None
        if mode not in {"m", "k"}:
            print("Nieprawidłowy wybór. Użyj M, K albo 'koniec'.")
            continue

        phrase = input("Podaj nazwę miasta, kod kraju (np. PL/DE) lub nazwę kraju: ").strip()
        if not phrase:
            print("Puste zapytanie. Spróbuj ponownie.")
            continue

        if mode == "k":
            country_part, city_part = split_country_city_query(phrase)
            if country_part:
                matches = search_by_country_and_city(
                    stations,
                    countries=countries,
                    country_query=country_part,
                    city_query=city_part,
                )
            else:
                matches = search_by_country(stations, phrase, countries=countries)
        else:
            country_part, city_part = split_country_city_query(phrase)
            if country_part:
                matches = search_by_country_and_city(
                    stations,
                    countries=countries,
                    country_query=country_part,
                    city_query=city_part,
                )
            else:
                matches = search_by_city(stations, phrase)

        selected = choose_station_id(matches, max_results)
        if selected == "":
            return None
        if selected:
            return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wyszukiwanie stacji NOAA GHCN i wybór ID.")
    parser.add_argument("--mode", choices=["city", "country"], help="Tryb wyszukiwania.")
    parser.add_argument("--query", help="Fraza wyszukiwania: nazwa miasta lub kod kraju.")
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Maksymalna liczba pokazywanych wyników (domyślnie: {DEFAULT_MAX_RESULTS}).",
    )
    parser.add_argument(
        "--auto-select-top",
        action="store_true",
        help="Automatycznie wybiera najwyżej oceniony wynik bez interakcji.",
    )
    return parser.parse_args()


def find_stations(
    mode: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    auto_select_top: bool = False,
) -> Optional[str]:
    stations = load_stations()
    if stations is None:
        return None
    countries = load_countries()

    max_results = max(1, max_results)

    if mode and query:
        if mode == "country":
            country_part, city_part = split_country_city_query(query)
            if country_part:
                results = search_by_country_and_city(
                    stations,
                    countries=countries,
                    country_query=country_part,
                    city_query=city_part,
                )
            else:
                results = search_by_country(stations, query, countries=countries)
        else:
            country_part, city_part = split_country_city_query(query)
            if country_part:
                results = search_by_country_and_city(
                    stations,
                    countries=countries,
                    country_query=country_part,
                    city_query=city_part,
                )
            else:
                results = search_by_city(stations, query)

        selected = choose_station_id(results, max_results, auto_select_top=auto_select_top)
        if selected == "":
            return None
        return selected

    return interactive_station_picker(stations, countries=countries, max_results=max_results)


if __name__ == "__main__":
    args = parse_args()
    station_id = find_stations(
        mode=args.mode,
        query=args.query,
        max_results=args.max_results,
        auto_select_top=args.auto_select_top,
    )
    if station_id:
        print(f"\nWybrano stację: {station_id}")
    else:
        print("\nNie wybrano stacji.")
