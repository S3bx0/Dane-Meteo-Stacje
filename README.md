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

## Struktura projektu

- `dane_meteo_stacje/cli.py` — interfejs wiersza poleceń
- `dane_meteo_stacje/data.py` — przykładowe dane stacji
- `tests/` — testy regresyjne

## Testy

```bash
python -m pytest -q
```

## Licencja

Projekt jest udostępniany na licencji MIT.
