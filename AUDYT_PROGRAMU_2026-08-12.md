# Audyt programu Dane-Meteo-Stacje

Data audytu: 2026-08-12
Audytowana kopia: `X:\Projekty\Projekty instalacji\_Obliczenia\Obliczenia\Dane-Meteo-Stacje`
Gałąź: `main`
Ostatni commit bazowy: `117070d316e3a1ef45a62efc3ea26a4c3ac1aa03` (`0.1.1`)

## Status po wdrożeniu poprawek — 2026-08-17

Najważniejsze zalecenia audytu zostały wdrożone i zweryfikowane lokalnie:

| Obszar | Status po poprawce |
|---|---|
| Prywatny `.env` | domyślna lokalizacja: `%LOCALAPPDATA%\Dane-Meteo-Stacje\.env`; plik projektu tylko jako zgodność wsteczna |
| Tokeny NOAA | współdzielony provider, trwałe cooldowny, rotacja przez całą pulę i odstęp 0,26 s per token |
| Całkowita awaria temperatur | zwracany jest błąd NOAA; nie powstaje wiarygodnie wyglądający JSON pełny `null` |
| Cache | atomowy cache katalogu kraju i temperatur per stacja/rok; stary katalog może być fallbackiem |
| Jakość danych | odrzucane są obserwacje z niepustym NOAA QFLAG |
| Stacje | zapytanie katalogu wymaga TMAX i TMIN; GUI pokazuje `mindate`, `maxdate` i `datacoverage` |
| GUI | backend generuje listę obsługiwanych krajów, tabela ma porcje po 250 wierszy, nazwa eksportu pochodzi od miasta |
| Lokalne API | wymagany `application/json`, kontrola `Origin`, blokada bindu poza loopback bez `--allow-network` |
| Readiness | bez lokalnego tokenu zwraca `503 not_ready` |
| Test E2E | poprawiona etykieta i dodany scenariusz kraj → wybór stacji → JSON Heatmapy |
| Higiena | logi `*.log` są ignorowane; User-Agent korzysta z wersji pakietu |

Końcowa walidacja przed wydaniem 0.2.0: **205 testów zaliczonych, coverage 93,17%**.
Ruff, mypy, Bandit, pip-audit, test Playwright E2E oraz CodeQL zakończyły się bez zgłoszeń
w GitHub Actions na `main`.

Tokeny lokalne są skonfigurowane, a aplikacja potwierdziła działanie zapytań NOAA. Zalecana
pozostaje prywatna lokalizacja `%LOCALAPPDATA%\\Dane-Meteo-Stacje\\.env`; projektowy `.env`
jest obsługiwany wyłącznie dla zgodności wstecznej i pozostaje ignorowany przez Git.

Zmiany funkcjonalne i bezpieczeństwa zostały scalone do `main` w PR #8. Pozostałe usprawnienia
nieblokujące stabilnego użycia: praca temperatur w tle z postępem i anulowaniem, bezpośredni
zapis do katalogu Heatmapy oraz automatyczna retencja cache/logów.

## Podsumowanie pierwotnego audytu

Program ma poprawną podstawę techniczną i aktualny przepływ wyszukiwania kraju oraz eksportu
JSON jest znacznie bliższy założeniom projektu. Domyślne uruchomienie na `127.0.0.1`, ochrona
przed wysłaniem tokenu poza domenę NOAA, limity rozmiaru żądań, OpenAPI, typowanie i testy są
dobrymi elementami projektu.

Aktualnej wersji nie należy jeszcze wysyłać na `main` bez poprawek. CI jest obecnie blokowane
przez coverage, test E2E używa starej etykiety formularza, a nowe wymagane pliki pozostają
nieśledzone. Najważniejsze problemy wykonawcze dotyczą limitowania NOAA, obsługi awarii podczas
pobierania temperatur, braku cache i wybierania stacji bez danych temperaturowych.

Najpilniejsza kwestia bezpieczeństwa nie leży w samym parserze `.env`, lecz w miejscu zapisania
pliku. Dysk `X:` jest udziałem sieciowym, a odziedziczone ACL pliku `.env` przyznają odczyt lub
modyfikację wielu kontom i grupom. Zawartość `.env` nie była odczytywana podczas audytu.

## Wyniki automatyczne

| Kontrola | Wynik | Uwagi |
|---|---:|---|
| Pytest, bez E2E | 145 zaliczonych, 1 pominięty | Test E2E pominięty z powodu braku Playwright/Chromium |
| Coverage jak w CI | **91,44% — błąd** | Workflow wymaga 93% |
| Ruff | zaliczony | Brak zgłoszeń |
| mypy | zaliczony | Brak zgłoszeń w 10 modułach |
| Bandit | zaliczony | Brak zgłoszeń dla `dane_meteo_stacje/` |
| `git diff --check` | zaliczony | Ostrzeżenia tylko o przyszłej zamianie LF na CRLF |
| `pip-audit` środowiska | 6 podatności w `pip 25.0.1` | Dotyczy narzędzia instalacyjnego, nie zależności runtime; zalecana aktualizacja `pip` |
| `pip-audit .` | nieukończony | Timeout przy tymczasowym środowisku Windows/aliasie krótkiej ścieżki |
| Budowa paczki | niepotwierdzona lokalnie | Izolowany build czekał na sieć; build bez izolacji nie miał `setuptools` w venv |
| Działający serwer | gotowy | `127.0.0.1:8765`, `/health/ready` zwrócił HTTP 200 |
| Test rzeczywistego NOAA | niewykonany | Lokalny interfejs zgłasza brak skonfigurowanego tokenu; sekretów nie odczytywano |

## Ustalenia o wysokim priorytecie (P1)

### A-01. Plik `.env` na udziale sieciowym ma zbyt szerokie uprawnienia

**Ryzyko:** wysokie, jeżeli zostaną w nim zapisane prawdziwe tokeny.
**Dowód:** ACL zawiera ponad 20 odziedziczonych wpisów. Wiele tożsamości ma
`ReadAndExecute`, a część `Modify` lub `FullControl`.
**Skutek:** inny użytkownik udziału może odczytać, zmienić albo usunąć tokeny NOAA.

**Zalecenie:** przechowywać prawdziwy plik poza udziałem, na przykład:

```text
%LOCALAPPDATA%\Dane-Meteo-Stacje\.env
```

i wskazywać go przez `DANE_METEO_ENV_FILE`. Alternatywnie zawęzić ACL wyłącznie do konta
użytkownika i kont systemowych. Docelowo warto użyć Windows Credential Manager. `.env.example`
może pozostać w repozytorium, ponieważ nie zawiera sekretów.

### A-02. Obecne zmiany nie przechodzą progu coverage w GitHub Actions

**Dowód:** `.github/workflows/ci.yml:45` wymaga `--cov-fail-under=93`; identyczna lokalna komenda
uzyskała `91,44%` i zakończyła się kodem błędu mimo 145 zaliczonych testów.
**Skutek:** push lub pull request zostanie zablokowany.

**Zalecenie:** dodać testy brakujących gałęzi, zwłaszcza nowych błędów NOAA, pełnej rotacji
tokenów, awarii wszystkich lat, paginacji, walidacji krajów oraz nowego GUI. Nie obniżać progu
bez świadomej decyzji.

### A-03. Test E2E jest niezgodny z aktualnym formularzem

**Dowód:** `tests/test_gui_e2e.py:49` szuka etykiety `Query (optional)`, a GUI używa obecnie
`Name or city filter (optional)`. Test E2E został lokalnie pominięty, więc problem nie pojawił
się w zwykłym pytest.
**Skutek:** zadanie `e2e` w GitHub Actions prawdopodobnie nie przejdzie.

**Zalecenie:** zaktualizować selektory i dodać E2E dla:

- `Spain` z pustym Station ID;
- wyboru stacji;
- pobierania JSON temperatur;
- błędu `NOAA_AUTH` i statusu `.env`.

### A-04. Nowe wymagane pliki są nieśledzone, a cała poprawka jest niezatwierdzona

**Dowód:** repozytorium jest na `main`, ma 894 dodane linie i 93 usunięte linie w plikach
śledzonych. `dane_meteo_stacje/countries.py`, `tests/test_noaa_workflow.py` i `.env.example`
są nieśledzone.
**Skutek:** użycie `git add -u` pominie `countries.py`, przez co commit nie będzie się importował.
Nie ma też kopii zmian na GitHubie.

**Zalecenie:** po poprawieniu ustaleń P1 utworzyć gałąź `codex/...`, dodać wszystkie wymagane
pliki, przejrzeć diff, uruchomić CI i dopiero wtedy scalić.

### A-05. Sterowanie limitami i rotacja puli tokenów nie są kompletne

**Dowód:** `data.py:980` domyślnie uruchamia 6 równoległych pobrań. NOAA ogranicza każdy token
do 5 zapytań na sekundę i 10 000 dziennie. `data.py:618-634` wykonuje najwyżej jedną zmianę
tokenu po błędzie, więc przy puli większej niż dwa tokeny trzeci token nie zostanie wypróbowany.
Nowy `TokenProvider` powstaje dla kolejnych żądań HTTP, więc cooldown i kwarantanna nie są
współdzielone między żądaniami.

**Skutek:** niepotrzebne HTTP 429, niewykorzystanie całej puli, chwilowe błędy przy poprawnych
tokenach oraz szybsze zużycie dziennego limitu.

**Zalecenie:** wprowadzić jeden współdzielony menedżer tokenów, limiter per token do maksymalnie
5 req/s (praktycznie 4 req/s z marginesem), pętlę próbującą wszystkie aktywne tokeny,
obsługę `Retry-After`, pełny jitter/backoff i trwały cooldown.

Źródło limitów: https://www.ncdc.noaa.gov/cdo-web/webservices/v2

### A-06. Awaria NOAA może zostać zapisana jako poprawny JSON pełen `null`

**Dowód:** `data.py:1024-1027` zamienia każdy `NoaaClientError` dla roku na wpis w raporcie,
a następnie `data.py:1050-1065` zawsze buduje wynik. Jeżeli wszystkie lata zawiodą przez sieć,
rate limit lub odrzucony token, endpoint może zwrócić HTTP 200 z samymi `null`.

**Skutek:** użytkownik i Heatmapa mogą potraktować błąd pobierania jak prawdziwy brak danych
klimatycznych.

**Zalecenie:** rozdzielić stany `no_data` i `failed`. Gdy nie powiódł się żaden rok z powodu
błędu technicznego, zwrócić kontrolowany błąd API. Dla częściowego sukcesu dodać jawny status
`partial` i licznik lat pobranych, bez ukrywania przyczyny.

### A-07. Wyszukiwanie nie gwarantuje danych temperatury

**Dowód:** zapytanie stacji w `data.py:803-810` filtruje tylko `datasetid=GHCND` i kraj. Nie
sprawdza `TMAX`, `TMIN` ani zakresu danych. NOAA podaje, że wiele stacji GHCN-Daily raportuje
tylko opady. GUI nie pokazuje `mindate`, `maxdate` ani `datacoverage`, mimo że backend je
zachowuje.

**Skutek:** użytkownik może wybrać stację, która nie ma temperatur, albo ustawić lata poza jej
zakresem i dostać plik z lukami.

**Zalecenie:** oferować filtr „tylko stacje z temperaturą”, pokazywać zakres lat i pokrycie,
automatycznie proponować zakres wspólny dla stacji oraz oznaczać stacje o słabej kompletności.
Rozważyć katalog `ghcnd-stations.txt` + `ghcnd-inventory.txt`, który może być lokalnie
indeksowany bez zużywania tokenu CDO.

Źródła NOAA:

- https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily
- https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-countries.txt

### A-08. Nazwa eksportowanego pliku jest niezgodna semantycznie z Heatmapą

**Dowód:** `gui_bootstrap.py:653` zapisuje `<station_id>_temperatures.json`. Heatmapa w
`heatmap_app/data_loader.py` wyprowadza nazwę miasta z nazwy pliku, a nie z `station_id`
wewnątrz JSON.

**Skutek:** plik zostanie odczytany, ale na liście miast pojawi się kod stacji, np.
`GME00111445`, zamiast `Berlin` lub `Madrid`.

**Zalecenie:** generować bezpieczną nazwę z wybranej nazwy miasta/stacji, np.
`madrid_retiro_temperatures.json`. Dodać opcjonalny bezpośredni zapis do
`Heatmapa/data/cities` oraz walidację pliku po zapisie.

## Ustalenia średniego priorytetu (P2)

### A-09. Brak cache dla najdroższych nowych operacji

Automatyczne wyszukiwanie kraju (`fetch_stations_for_country`) omija istniejącą politykę cache,
a temperatury nie są cache'owane wcale. Ponowienie tego samego wyszukiwania lub eksportu
wykonuje wszystkie zapytania ponownie. Należy dodać cache per kraj oraz per
`station_id + rok`, z TTL, atomowym zapisem i blokadą współbieżnych zapisów.

### A-10. GUI oferuje 20 krajów, których backend celowo nie obsługuje

Porównanie `COUNTRY_OPTIONS` i `COUNTRY_FIPS_CODES` wykazało 195 opcji GUI oraz 175 mapowań.
Zawsze błędne opcje to:

`Andorra`, `Bhutan`, `Comoros`, `Djibouti`, `Grenada`, `Haiti`, `Liechtenstein`, `Monaco`,
`Nauru`, `Palestine`, `Saint Kitts and Nevis`, `Saint Vincent and the Grenadines`, `Samoa`,
`San Marino`, `Sao Tome and Principe`, `Somalia`, `South Sudan`, `Timor-Leste`, `Vatican City`,
`Yemen`.

Lista GUI powinna być generowana z jednego źródła backendu, najlepiej z oficjalnego katalogu
NOAA. Nie należy utrzymywać dwóch ręcznych list.

### A-11. Limit czasu opisany w dokumentacji nie jest limitem całej operacji

`OPERATIONS.md` mówi o 15 sekundach dla całej operacji. W kodzie deadline jest tworzony osobno
dla każdego wywołania `NoaaClient.fetch_json`. Paginacja kraju może więc zużyć wielokrotność
15 sekund, a eksport temperatur ma do 30 sekund na stronę każdego roku.

Należy wprowadzić deadline całego zadania i przekazywać pozostały budżet do kolejnych stron,
albo poprawić dokumentację i pokazywać postęp oraz możliwość anulowania.

### A-12. Pobranie „wszystkich stacji” może przeciążyć przeglądarkę

Backend zwraca pełną listę, a `renderResults` tworzy w DOM jeden wiersz dla każdej stacji.
Dla dużych państw odpowiedź i tabela mogą być bardzo duże. Pole `Limit` ogranicza dopiero wynik
po pobraniu pełnego katalogu.

Zalecane są: paginacja/virtual scrolling, osobny licznik wszystkich wyników, limit widoku oraz
eksport pełnej listy niezależny od renderowania tabeli.

### A-13. Lokalizacja `.env` zależy od bieżącego katalogu procesu

`data.py:139` używa `Path.cwd() / ".env"`. Obecny skrót startowy ustawia właściwy katalog, ale
uruchomienie polecenia z innego folderu przestanie widzieć `.env`.

Należy dodać parametr CLI `--env-file`, jasno pokazywać faktycznie używaną lokalizację bez
ujawniania wartości oraz preferować prywatny katalog konfiguracyjny użytkownika.

### A-14. Readiness zwraca 200 bez tokenu i bez sprawdzenia NOAA

`gui_bootstrap.py:808` zawsze raportuje `noaa: request-scoped`. Jest to poprawne jako liveness,
ale mylące jako readiness funkcji wymagającej tokenu. Warto dodać osobny stan
`configured/degraded`, nadal bez wykonywania kosztownego zapytania do NOAA.

### A-15. Brak ochrony Origin/Content-Type dla lokalnego API

Serwer domyślnie jest bezpiecznie przypięty do loopback, ale parser POST nie wymaga
`application/json` i nie sprawdza `Origin`. Złośliwa strona otwarta w przeglądarce może próbować
wysyłać proste żądania do localhost i zużywać limit NOAA, nawet jeśli nie odczyta odpowiedzi.
Uruchomienie z `--host 0.0.0.0` dodatkowo wystawia API bez uwierzytelnienia w sieci.

Należy wymagać `Content-Type: application/json`, odrzucać obce `Origin`, generować lokalny token
sesji albo uniemożliwić bind poza loopback bez jawnej flagi ostrzegawczej.

### A-16. Jakość klimatologiczna wymaga jawnego opisu

Miesięczna wartość jest obliczana jako średnia miesięcznego TMAX i miesięcznego TMIN. To zgadza
się z obecnym modułem `Heatmapa/noaa_fetch/processing.py`, ale może być obciążone błędem, gdy
TMAX i TMIN mają różne brakujące dni. Atrybuty jakości NOAA nie są filtrowane.

Należy udokumentować metodę, rozważyć TAVG, liczyć średnią dobową tylko dla sparowanych dni,
odrzucać obserwacje z nieakceptowanymi flagami jakości i zapisywać liczebność obserwacji.

## Ustalenia niskiego priorytetu (P3)

- Cache stacji jest zapisywany bez atomowej zamiany i blokady; dwa równoległe zapisy mogą się
  zderzyć (`data.py:1175`).
- Zakres dopuszcza `current_year + 1`, więc użytkownik może świadomie wygenerować przyszły rok
  pełen braków (`data.py:986-988`).
- Nagłówek NOAA jest wysyłany jednocześnie jako `token` i `Authorization: Bearer`; oficjalna
  dokumentacja wymaga nagłówka `token`, więc drugi nagłówek jest zbędny.
- `NoaaClient` ma User-Agent `dane-meteo-stacje/0.2`, ale wersja pakietu to `0.1.1`.
- `server*.log` nie są ignorowane i zaśmiecają status Git.
- Repozytorium nadal śledzi `dane_meteo_stacje/__pycache__/__init__.cpython-314.pyc`.
- `verify_ID.py` i `test_verify_ID.py` są wyłączone z Ruff, a Bandit skanuje tylko pakiet;
  pozostaje równoległa, starsza implementacja wyszukiwania poza głównymi kontrolami jakości.
- Brak pliku constraints/lock powoduje, że lokalne instalacje i CI mogą dostać inne wersje
  zależności.
- E2E nie obejmuje nowego panelu temperatur ani integracji z Heatmapą.

## Mocne strony

- Domyślny bind tylko do `127.0.0.1`.
- Token nie jest wysyłany poza hosty `*.noaa.gov`, także po przekierowaniu.
- Zdalny URL musi używać HTTPS i rozwiązywać się wyłącznie do publicznych adresów IP.
- `.env` jest ignorowany przez Git; w śledzonych plikach nie wykryto przypisanych sekretów.
- Strukturalne logi i metryki nie zapisują surowych tokenów ani parametrów użytkownika.
- Limit rozmiaru żądania, kontrolowana walidacja JSON i ograniczenie liczby ciężkich requestów.
- CSP, `nosniff`, `Referrer-Policy`, `Permissions-Policy` i `Cache-Control: no-store`.
- OpenAPI 3.1 oraz spójne kody diagnostyczne.
- Ruff, mypy i Bandit przechodzą bez błędów.
- Format danych JSON jest zgodny strukturalnie z Heatmapą: lata, 12 miesięcy, macierz,
  raport braków i metadane pobrania.
- GitHub Actions są przypięte do konkretnych SHA; repo ma CodeQL, Dependabot, politykę
  bezpieczeństwa i workflow sprawdzający sekret NOAA.

## Zalecana kolejność prac

### Etap 1 — przed jakimkolwiek push do `main`

1. Przenieść `.env` do prywatnego katalogu lub zawęzić ACL.
2. Naprawić coverage do minimum 93%.
3. Naprawić i rozszerzyć testy E2E.
4. Dodać wszystkie nowe wymagane pliki do Git, ignorować logi i usunąć śledzony `.pyc`.
5. Uruchomić pełne CI na osobnej gałęzi.

### Etap 2 — poprawność i niezawodność NOAA

1. Współdzielony menedżer tokenów, limiter per token, wszystkie tokeny, `Retry-After` i backoff.
2. Rozdzielenie `no_data`, `partial` i `failed`; nigdy HTTP 200 z samymi `null` po awarii.
3. Cache katalogu stacji i temperatur per rok.
4. Filtr stacji z TMAX/TMIN i dobór zakresu lat.
5. Globalny deadline, postęp i anulowanie długiego zadania.

### Etap 3 — GUI i Heatmapa

1. Generować listę krajów z backendu/oficjalnego katalogu NOAA.
2. Pokazywać zakres danych, pokrycie, wysokość i status dostępności temperatur.
3. Dodać paginację lub wirtualizację tabeli.
4. Nazywać plik według miasta/stacji i opcjonalnie zapisywać bezpośrednio do Heatmapy.
5. Uruchomić walidator Heatmapy po wygenerowaniu pliku i pokazać procent kompletności.

### Etap 4 — utrzymanie

1. Ujednolicić wersję pakietu i User-Agent.
2. Dodać constraints/lock i zaktualizować lokalny `pip`.
3. Włączyć starsze skrypty do kontroli albo przenieść je do `legacy/` i jasno oznaczyć.
4. Dodać automatyczną rotację logów i okresowe czyszczenie cache.

## Rekomendacja końcowa

Program nadaje się do dalszego lokalnego testowania, ale nie jest jeszcze gotowy do stabilnego
wydania ani niepilnowanego wieloletniego pobierania. Największą wartość przyniesie najpierw
naprawa CI i bezpieczeństwa tokenów, następnie niezawodna obsługa limitów/awarii NOAA, a dopiero
potem rozbudowa GUI. Po wykonaniu Etapów 1 i 2 warto powtórzyć audyt oraz wykonać kontrolowany
test live NOAA na małym zakresie jednego roku i jednej stacji.
