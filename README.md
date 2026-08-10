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
- kontrolowanego fallbacku przy błędach NOAA,
- diagnostyki źródła danych i kodów błędów,
- łatwego integrowania z innymi narzędziami analitycznymi lub skryptami.

Obecnie repo zawiera przykładowe dane dla kilku stacji oraz obsługę danych zewnętrznych i cache.

## Instalacja

```bash
python -m pip install .
```

Do pracy nad kodem z zaleznosciami deweloperskimi:

```bash
python -m pip install -e .
python -m pip install -e .[dev]
```

## Użycie

### GUI (Bootstrap)

```bash
python -m dane_meteo_stacje.gui_bootstrap
```

Lub po instalacji skryptu:

```bash
dane-meteo-stacje-gui
dane-meteo-stacje-gui --log-level DEBUG
```

Ze wzgledow bezpieczenstwa pole `Remote URL` w GUI akceptuje wylacznie adresy HTTPS
w domenie `noaa.gov`. Interfejs CLI obsluguje tez zewnetrzne zrodla JSON podane przez
`--remote-url`, ale wymaga HTTPS i publicznych adresow sieciowych. Kazde przekierowanie
jest ponownie sprawdzane, a token NOAA nigdy nie jest wysylany poza domene `noaa.gov`.
Pole `Cache File` w GUI przyjmuje tylko nazwe pliku z rozszerzeniem `.json`; plik jest
przechowywany w `~/.cache/dane-meteo-stacje`. CLI nadal pozwala jawnie wskazac lokalna
sciezke cache argumentem `--cache`.

GUI zapisuje logi operacyjne jako pojedyncze obiekty JSON. Kazda odpowiedz API zawiera
ten sam identyfikator korelacyjny w polu `request_id` i naglowku `X-Request-ID`, co pozwala
powiazac blad widoczny w kliencie z logami serwera. Nieoczekiwany blad zwraca bezpieczny
kod `INTERNAL_ERROR`, a szczegoly i traceback pozostaja w logach serwera. Log zakonczenia
zadania zawiera czas obslugi `duration_ms`. Tokeny NOAA nie sa logowane.

Serwer dopuszcza maksymalnie cztery rownolegle operacje pobierania stacji. Kolejne zadanie
otrzymuje `503 SERVER_BUSY` i moze zostac ponowione; `/health` oraz eksport nie sa blokowane
przez ten limit. Watki obslugi polaczen nie blokuja zatrzymania procesu serwera.

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

### Niezawodność i fallback

```bash
python -m dane_meteo_stacje search Bialystok --remote-url https://example.com/stations.json --stale-if-error
python -m dane_meteo_stacje search Bialystok --remote-url https://example.com/stations.json --allow-sample-fallback
python -m dane_meteo_stacje search Bialystok --remote-url https://example.com/stations.json --allow-sample-fallback --verbose
```

### Tokeny NOAA

```bash
# zalecane: CSV tokenow (kompatybilne z GitHub Secret NOAA_API_TOKENS)
set NOAA_API_TOKENS=token_a,token_b,token_c

# alternatywa: pula tokenow
set NOAA_TOKENS=token_a,token_b,token_c

# pojedynczy token
set NOAA_TOKEN=twoj_token
```

Priorytet odczytu tokenow: `NOAA_API_TOKENS` -> `NOAA_TOKENS` -> `NOAA_TOKEN`.

### Troubleshooting i kody diagnostyczne

Przy problemach z NOAA CLI zwraca kod wyjścia 2 oraz komunikat w stderr z kodem błędu:

- [error][NOAA_AUTH] - błędny token lub brak uprawnień (401/403)
- [error][NOAA_RATE_LIMIT] - przekroczony limit zapytań (429)
- [error][NOAA_NETWORK] - problemy sieciowe lub chwilowa niedostępność API
- [error][NOAA_PAYLOAD] - nieobsługiwany format odpowiedzi NOAA

Przy fallbackach wypisywane są ostrzeżenia:

- [warning][FALLBACK_STALE_CACHE] - użyto przeterminowanego cache
- [warning][FALLBACK_SAMPLE] - użyto danych przykładowych

W trybie --verbose pojawia się też wpis [debug] z metadanymi fetch_source i fetch_metadata.

## Struktura projektu

- `dane_meteo_stacje/cli.py` — interfejs wiersza poleceń
- `dane_meteo_stacje/data.py` — przykładowe dane stacji
- `dane_meteo_stacje/diagnostics.py` — wspolne kody i komunikaty bledow NOAA
- `dane_meteo_stacje/observability.py` — strukturalne logi i correlation ID
- `tests/` — testy regresyjne

## Współpraca z Heatmapa

Projekt jest projektowany jako pomocniczy moduł dla workflowu z Heatmapa. W praktyce:

- pozwala znaleźć odpowiednie ID stacji NOAA dla danego miasta lub kraju,
- dostarcza metadane stacji, które można wykorzystać przy przygotowaniu danych wejściowych,
- ułatwia późniejsze generowanie lub mapowanie danych do formatu używanego przez Heatmapa.

W przyszłości dane z tego narzędzia mogą być używane jako warstwa wejściowa do przygotowania plików JSON dla Heatmapa, np. przy definiowaniu źródła stacji dla konkretnego miasta.

## Testy

```bash
python -m pip install -e .[dev]
python -m pytest -m "not e2e" -q
```

Testy kontraktow API sprawdzaja spojny format odpowiedzi i correlation ID, a testy
wlasnosciowe Hypothesis obejmuja normalizacje danych, wyszukiwanie oraz serializacje.
Opcjonalne scenariusze przegladarkowe mozna uruchomic osobno:

```bash
python -m pip install -e .[e2e]
python -m playwright install chromium
python -m pytest tests/test_gui_e2e.py -q
```

CI blokuje zmiany, ktore nie przechodza Ruff, mypy, testow z coverage co najmniej 93%,
skanu kodu Bandit albo audytu zaleznosci pip-audit. Osobny workflow `NOAA Smoke`
codziennie sprawdza rzeczywiste API NOAA z sekretem `NOAA_API_TOKENS`; mozna go tez
uruchomic recznie w GitHub Actions.

CI uruchamia testy kompatybilnosci na Pythonie 3.10, 3.12 i 3.14 oraz osobne testy E2E
w Chromium przez Playwright. Buduje tez wheel oraz
archiwum zrodlowe, sprawdza ich metadane i instaluje wheel w czystym srodowisku. Gotowe
pliki sa dostepne w artefakcie `python-distributions` danego runu GitHub Actions.

Wydania sa tworzone automatycznie po wypchnieciu taga `vX.Y.Z`, jezeli tag jest zgodny
z wersja pakietu i wszystkie testy wydania przejda. Procedura znajduje sie w `RELEASING.md`,
a historia zmian w `CHANGELOG.md`. Wydanie zawiera wheel, sdist, CycloneDX SBOM, sumy SHA-256
i GitHub Artifact Attestation. Samo wypchniecie zmian na `main` nie publikuje wydania.

Zewnetrzne GitHub Actions sa przypiete do pelnych commit SHA. CodeQL analizuje kod Pythona
na `main`, w pull requestach i wedlug tygodniowego harmonogramu. GitHub Secret Scanning
z push protection oraz aktualizacje bezpieczenstwa Dependabota sa wlaczone dla repozytorium.

## Zrodla danych i noty

- Projekt korzysta z danych stacji publikowanych przez NOAA/NCEI API.
- Warunki uzycia i limity API nalezy sprawdzac w oficjalnej dokumentacji NOAA: https://www.ncei.noaa.gov/cdo-web/webservices/v2
- Projekt nie jest powiazany organizacyjnie z NOAA.
- Dane zrodlowe sa dostarczane przez dostawce zewnetrznego i moga sie zmieniac niezaleznie od tego repozytorium.
- Dodatkowe informacje prawne i noty zrodlowe znajduja sie w pliku NOTICE.

## Licencja

Projekt jest udostępniany na licencji MIT.
Szczegoly dotyczace zrodel danych i zaleznosci zewnetrznych: patrz plik NOTICE.
