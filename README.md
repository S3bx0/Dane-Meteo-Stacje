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

W GUI wpisanie kraju (np. `Poland` albo `Polska`) automatycznie buduje zapytanie
NOAA GHCND z `locationid=FIPS:PL` i pobiera wszystkie strony katalogu stacji.
Po wybraniu stacji można podać pierwszy i ostatni rok oraz pobrać JSON zgodny z
Heatmapą (`years`, `months`, macierz `temperatures`, raport braków i statystyki
wykorzystania zamaskowanych tokenów).

Do rzeczywistego wyszukiwania i pobierania temperatur proces GUI musi otrzymać
co najmniej jeden token przez `NOAA_API_TOKENS`, `NOAA_TOKENS` albo `NOAA_TOKEN`.
GitHub Actions przekazuje sekret `NOAA_API_TOKENS` tylko wewnątrz workflowu;
lokalne uruchomienie wymaga ustawienia tej samej zmiennej w środowisku procesu.

Najbezpieczniej przechowywać tokeny poza repozytorium, w prywatnym pliku użytkownika
`%LOCALAPPDATA%\Dane-Meteo-Stacje\.env`:

```powershell
$tokenDir = Join-Path $env:LOCALAPPDATA "Dane-Meteo-Stacje"
New-Item -ItemType Directory -Force $tokenDir
Copy-Item .env.example (Join-Path $tokenDir ".env")
```

Następnie wpisz tokeny po znaku `=`. Program automatycznie wczytuje zmiany bez
restartu. Można wskazać inną lokalizację przez `DANE_METEO_ENV_FILE`. Dla zgodności
wstecznej obsługiwany jest też `.env` w katalogu aplikacji, ale na dysku sieciowym
nie jest on zalecany. Żadnego pliku z tokenami nie wolno commitować ani wysyłać do
GitHuba. Zmienne procesu mają pierwszeństwo przed wartościami z pliku.

Ze wzgledow bezpieczenstwa pole `Remote URL` w GUI akceptuje wylacznie adresy HTTPS
w domenie `noaa.gov`. Interfejs CLI obsluguje tez zewnetrzne zrodla JSON podane przez
`--remote-url`, ale wymaga HTTPS i publicznych adresow sieciowych. Kazde przekierowanie
jest ponownie sprawdzane, a token NOAA nigdy nie jest wysylany poza domene `noaa.gov`.
Pole `Cache File` w GUI przyjmuje tylko nazwe pliku z rozszerzeniem `.json`; plik oraz
automatyczne cache katalogu krajów i temperatur są przechowywane w prywatnym katalogu
`%LOCALAPPDATA%\Dane-Meteo-Stacje\cache`. Zapisy cache są atomowe. CLI nadal pozwala
jawnie wskazać lokalną ścieżkę cache argumentem `--cache`.

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

Po wyszukaniu kraju i wybraniu stacji GUI automatycznie pyta NOAA o dostępne typy
danych temperatury. Dotychczasowy tryb **Heatmapa (zgodność wsteczna)** nadal
zapisuje dokładnie ten sam układ: `station_id`, `years`, 12 nazw miesięcy, macierz
`temperatures` w układzie rok × miesiąc, `final_missing_years`,
`missing_data_report`, `token_usage` oraz `adaptive_history`. Prefiks `GHCND:` jest
usuwany z `station_id`, tak jak w istniejących plikach `*_temperatures.json`.

GUI zawiera interaktywną mapę Leaflet z podkładem OpenStreetMap. Po wyszukaniu kraju mapa
pokazuje wszystkie stacje ze współrzędnymi z aktualnego wyniku, grupuje blisko położone
punkty i automatycznie dopasowuje widok. Kliknięcie klastra przybliża jego obszar, a
kliknięcie punktu wybiera tę samą stację do eksportu temperatur. Wybranie wiersza tabeli
przenosi mapę do stacji i otwiera jej opis. Przyciski pozwalają ponownie dopasować stacje
albo wrócić do widoku całego świata.

Biblioteki Leaflet i Leaflet.markercluster są dostarczane lokalnie wraz z aplikacją i nie
wymagają zewnętrznego CDN. Kafelki OpenStreetMap są pobierane wyłącznie dla aktualnie
widocznego obszaru, dlatego szczegółowy podkład mapy wymaga połączenia z internetem.

GUI udostępnia również trzy nowe eksporty:

- **Dzienne** — osobne wartości `TMIN`, raportowane przez NOAA `TAVG`, obliczane
  `TAXN`, `TMAX`, amplituda dobowa i flagi źródłowe; plik
  `*_daily_temperatures.json`.
- **Miesięczne** — osobne macierze `TMIN`, `TAVG`, `TAXN`, `TMAX` i amplitudy wraz
  z procentową kompletnością każdego elementu; plik `*_monthly_temperatures.json`.
- **Statystyki rozszerzone** — średnie, minima, maksima, amplituda, kompletność i
  porównanie `TAVG` z `TAXN`; plik `*_monthly_statistics.json`.

Każdy nowy plik zawiera `temperature_methods`. `TAVG` jest opisane jako wartość
raportowana przez NOAA, której metoda zależy od źródła. `TAXN` jest zawsze liczone
lokalnie według jawnego wzoru `(TMAX + TMIN) / 2`. Obserwacje z niepustą flagą
kontroli jakości NOAA nie uczestniczą w obliczeniach, a ich liczba trafia do
`quality_control`.

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

## Operacyjnosc HTTP

Serwer GUI udostepnia wersjonowany kontrakt i endpointy diagnostyczne:

- `GET /openapi.json` - kontrakt OpenAPI 3.1 zgodny z wersja pakietu,
- `GET /health/live` - lekki liveness procesu; `/health` pozostaje aliasem kompatybilnosci,
- `GET /health/ready` - readiness bez zapytania do NOAA; zwraca `503`, gdy brakuje lokalnego tokenu,
- `GET /metrics` - metryki Prometheus requestow, bledow, fallbackow, przeciazenia i czasu odpowiedzi.

Pobieranie katalogu stacji z NOAA ma 15-sekundowy budżet obejmujący wszystkie strony,
retry, backoff i przekierowania. Przekroczenie budżetu zwraca kontrolowane
`504 NOAA_TIMEOUT`. Żądania zapisu wymagają JSON i odrzucają obcy nagłówek `Origin`. Odpowiedzi
HTTP zawieraja CSP, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`,
`Cache-Control: no-store` oraz korelacyjny `X-Request-ID`.

Szczegoly retencji cache i logow, semantyka healthcheckow oraz procedura awarii NOAA sa
opisane w `OPERATIONS.md`.

## Zrodla danych i noty

- Projekt korzysta z danych stacji publikowanych przez NOAA/NCEI API.
- Interaktywny podkład mapowy korzysta z kafelków i danych © OpenStreetMap contributors.
- Publiczny serwer kafelków nie jest przeznaczony do pobierania hurtowego ani trybu offline.
- Warunki uzycia i limity API nalezy sprawdzac w oficjalnej dokumentacji NOAA: https://www.ncei.noaa.gov/cdo-web/webservices/v2
- Projekt nie jest powiazany organizacyjnie z NOAA.
- Dane zrodlowe sa dostarczane przez dostawce zewnetrznego i moga sie zmieniac niezaleznie od tego repozytorium.
- Dodatkowe informacje prawne i noty zrodlowe znajduja sie w pliku NOTICE.

## Licencja

Projekt jest udostępniany na licencji MIT.
Szczegoly dotyczace zrodel danych i zaleznosci zewnetrznych: patrz plik NOTICE.
