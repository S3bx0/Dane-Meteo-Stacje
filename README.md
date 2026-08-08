# Dane Meteo Stacje

Narzędzie do wyszukiwania i opisu przykładowych stacji meteorologicznych NOAA, z prostym interfejsem wiersza poleceń.

## Czym jest ten projekt?

Projekt dostarcza lekką warstwę CLI do:

- wyszukiwania stacji po nazwie miasta lub kraju,
- wyświetlania informacji o stacji,
- łatwego integrowania z innymi narzędziami analitycznymi lub skryptami.

Obecnie repo zawiera przykładowe dane dla kilku stacji i jest przygotowane pod dalszy rozwój.

## Instalacja

```bash
python -m pip install -e .
```

## Użycie

### Wyszukiwanie stacji

```bash
python -m dane_meteo_stacje search Bialystok
```

### Informacje o stacji

```bash
python -m dane_meteo_stacje info PLM00012295
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
