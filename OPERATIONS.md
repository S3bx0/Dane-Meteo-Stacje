# Operations

## Health checks

- `GET /health/live` potwierdza, ze proces obsluguje HTTP. Nie sprawdza NOAA ani dysku.
- `GET /health/ready` potwierdza gotowosc lokalnej aplikacji. NOAA jest zaleznoscia
  przekazywana dla pojedynczego requestu, a cache jest opcjonalny, dlatego ich chwilowa
  niedostepnosc nie wyklucza procesu z ruchu.
- `GET /health` jest zachowanym aliasem liveness dla starszych integracji.

Healthchecki nie wykonuja zewnetrznych polaczen i nie zwracaja tokenow, sciezek cache ani
innych danych wrazliwych.

## Request deadlines and overload

Pobieranie z NOAA ma laczny budzet 15 sekund. Budzet obejmuje proby HTTP, przekierowania
oraz backoff. Po jego przekroczeniu API zwraca `504` z kodem `NOAA_TIMEOUT`.

Maksymalnie cztery kosztowne pobrania moga trwac jednoczesnie. Kolejne zadanie oczekuje
0,1 sekundy na miejsce, a nastepnie otrzymuje `503 SERVER_BUSY`. Liveness i metryki nadal
dzialaja podczas przeciazenia.

## Metrics

`GET /metrics` zwraca tekstowy format Prometheus 0.0.4. Metryki obejmuja:

- requesty wedlug metody, znanej trasy i statusu,
- liczbe bledow wedlug statusu HTTP,
- laczny czas i liczbe requestow,
- fallbacki `cache-stale` i `sample-fallback`,
- odpowiedzi `SERVER_BUSY`,
- aktualna liczbe aktywnych fetchy.

Metryki sa przechowywane w pamieci procesu i zeruja sie po restarcie. Nie zawieraja tokenow,
parametrow wyszukiwania, adresow zdalnych ani danych stacji. Nieznane URL-e sa agregowane jako
`<other>`, aby ograniczyc cardinality.

## Cache retention

Pliki cache GUI sa ograniczone do katalogu `~/.cache/dane-meteo-stacje` i nazw z rozszerzeniem
`.json`. Aplikacja nie usuwa ich automatycznie. `cache_ttl` steruje swiezoscia, a
`max_stale_seconds` ogranicza wiek danych uzywanych przez jawny tryb `stale_if_error`.
Operator odpowiada za retencje plikow zgodnie z wymaganiami srodowiska.

## Log retention

Serwer emituje strukturalne logi JSON do skonfigurowanego strumienia. Aplikacja nie zapisuje
ani nie rotuje plikow logow; retencja nalezy do systemu uruchomieniowego, na przyklad journald,
kontenera albo zewnetrznego kolektora. Logi zawieraja `request_id` i typ bledu, ale nie surowe
tokeny NOAA.

## NOAA failure behavior

- `401/403` oznacza `NOAA_AUTH` i kwarantanne wadliwego tokenu.
- `429` oznacza `NOAA_RATE_LIMIT` oraz rotacje/cooldown tokenu.
- timeout calej operacji oznacza `NOAA_TIMEOUT` i status `504`.
- pozostale bledy sieci NOAA oznaczaja `NOAA_NETWORK` i status `502`.
- fallback do przeterminowanego cache lub danych przykladowych dziala tylko po jawnym
  wlaczeniu odpowiedniej opcji.

## Shutdown

`Ctrl+C` zatrzymuje petle serwera i zawsze zamyka socket nasluchujacy. Watki requestow sa
daemonami, wiec nie blokuja zakonczenia procesu. Przed restartem orchestrator powinien przestac
kierowac nowy ruch po negatywnym sygnale readiness i zapewnic wlasny okres wygaszania.