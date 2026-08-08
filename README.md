# Dane Meteo Stacje

Narzędzie do wyszukiwania i opisu stacji meteorologicznych NOAA z prostym interfejsem wiersza poleceń.

## Czym jest ten projekt?

Projekt dostarcza lekką warstwę CLI do:

- wyszukiwania stacji po nazwie miasta lub kraju,
- filtrowania wyników po kraju lub ID stacji,
- sortowania wyników według miasta, nazwy lub ID,
- wyświetlania informacji o stacji,
- eksportu danych do JSON lub CSV,
- pracy z lokalnym cache i zewnętrznym źródłem danych,
- łatwego integrowania z innymi narzędziami analitycznymi lub skryptami.

Obecnie repo zawiera przykładowe dane dla kilku stacji oraz obsługę danych zewnętrznych i cache.

## Instalacja

```bash
python -m pip install -e .
```

## Użycie

### Wyszukiwanie stacji

```bash
python -m dane_meteo_stacje search Bialystok
```

### Filtrowanie i sortowanie

```bash
python -m dane_meteo_stacje search Bialystok --country Poland
python -m dane_meteo_stacje search Bialystok --station-id PLM00012295
python -m dane_meteo_stacje search Bialystok --sort name
python -m dane_meteo_stacje search Bialystok --limit 5
```

### Informacje o stacji

```bash
python -m dane_meteo_stacje info PLM00012295
```

### Eksport danych

```bash
python -m dane_meteo_stacje export --output-json stations.json
python -m dane_meteo_stacje export --output-csv stations.csv --pretty
python -m dane_meteo_stacje export --output-json subset.json --station-id PLM00012295
python -m dane_meteo_stacje export --output-json noaa.json --noaa-like
```

### Po zainstalowaniu jako skrypt

```bash
dane-meteo-stacje search Bialystok
dane-meteo-stacje info PLM00012295
```

### Cache i NOAA

```bash
python -m dane_meteo_stacje search Bialystok --cache cache.json --remote-url https://example.com/stations.json
python -m dane_meteo_stacje search Bialystok --refresh --show-source
python -m dane_meteo_stacje cache-meta cache.json
```

## Struktura projektu

- `dane_meteo_stacje/cli.py` — interfejs wiersza poleceń
- `dane_meteo_stacje/data.py` — przykładowe dane stacji
- `tests/` — testy regresyjne

## Współpraca z Heatmapa

Projekt jest projektowany jako pomocniczy moduł dla workflowu z Heatmapa. W praktyce:

- pozwala znaleźć odpowiednie ID stacji NOAA dla danego miasta lub kraju,
- dostarcza metadane stacji, które można wykorzystać przy przygotowaniu danych wejściowych,
- ułatwia późniejsze generowanie lub mapowanie danych do formatu używanego przez Heatmapa.

W przyszłości dane z tego narzędzia mogą być używane jako warstwa wejściowa do przygotowania plików JSON dla Heatmapa, np. przy definiowaniu źródła stacji dla konkretnego miasta.

## Testy

```bash
python -m pytest -q
```

## Licencja

Projekt jest udostępniany na licencji MIT.
