# Changelog

Wszystkie istotne zmiany w projekcie sa dokumentowane w tym pliku.
Format jest oparty na Keep a Changelog, a wersje stosuja Semantic Versioning.

## [Unreleased]

## [0.2.0] - 2026-08-17

### Added

- Interaktywna mapa Leaflet z podkładem OpenStreetMap, grupowaniem znaczników, wyborem
  stacji z mapy i automatycznym przybliżeniem do jej współrzędnych.
- Automatyczny, atomowy cache katalogów krajów i rocznych danych temperatur.
- Zakres danych i pokrycie stacji w tabeli oraz stronicowanie dużych wyników.
- Ocena jakości stacji (dobra, średnia, słaba) uwzględniająca kompletność, długość
  okresu, aktualność oraz potwierdzone typy TMIN/TAVG/TMAX; filtry jakości i automatyczny
  wybór najlepszego kandydata.
- Podgląd ostatnich 1–10 lat przed eksportem: wykres TMIN/TAVG/TMAX, amplituda,
  brakujące dni, niepełne lata oraz porównanie najbliższych stacji.
- Porównywanie 2–5 stacji we wspólnym zakresie lat: wykres wybranego typu temperatury,
  średnie i różnice względem stacji bazowej, kompletność, braki, ranking jakości oraz
  macierz odległości geograficznych.
- Delikatne niebieskie podświetlenie obszaru wybranego kraju po jego granicach OSM,
  z 12% kryciem i 30-dniowym lokalnym cache geometrii.
- Automatyczne wykrywanie typów temperatury dostępnych dla wybranej stacji NOAA.
- Eksporty dzienne, miesięczne i rozszerzone z osobnym `TAVG`, obliczanym `TAXN`,
  amplitudą, kompletnością oraz metadanymi metody obliczenia.

### Changed

- Mapa ładuje od razu kafelki docelowego kraju lub stacji, bez wcześniejszego pobierania
  widoku świata i bez kosztownych animacji pośrednich; połączenie z serwerem kafelków jest
  zestawiane wcześniej, a aktualizacje podczas zoomu są odroczone do końcowego widoku.
- Znaczniki i klastry mapy pokazują jakość stacji kolorami, a legenda podaje liczby
  dobrych, średnich i słabych stacji po zastosowaniu filtrów.
- Schematyczne wielokąty kontynentów zastąpiono prawdziwym podkładem mapowym z nazwami,
  drogami i granicami państw; biblioteki mapy są dostarczane lokalnie z aplikacją.
- Trwała rotacja wszystkich tokenów NOAA, ograniczanie tempa i cache zachowywane między żądaniami.
- Eksport temperatur używa nazwy miasta zgodnej z konwencją Heatmapy.
- Lista krajów GUI jest generowana z mapowania obsługiwanego przez backend.
- Dotychczasowy JSON Heatmapy pozostaje domyślnym trybem zgodności wstecznej.

### Fixed

- Całkowita awaria NOAA zwraca błąd zamiast pozornie poprawnej macierzy pełnej `null`.
- Odrzucanie obserwacji temperatur oznaczonych przez NOAA flagą kontroli jakości.
- Test E2E używa aktualnej etykiety pola wyszukiwania.

### Security

- Domyślny plik tokenów przeniesiono do `%LOCALAPPDATA%\\Dane-Meteo-Stacje\\.env`.
- Lokalne API wymaga JSON, sprawdza `Origin` i domyślnie nie pozwala wystawić GUI poza loopback.
- GUI korzysta wyłącznie z zarządzanych zapytań NOAA i prywatnego cache; dowolne zdalne URL-e
  oraz ścieżki plików pozostają dostępne tylko dla lokalnego CLI.
- Żądania zdalne wymagają HTTPS, dokładnej domeny NOAA NCEI, publicznego adresu DNS i ponownej
  walidacji każdego przekierowania.
- Ścieżki cache kraju i temperatur są normalizowane i ograniczone do zarządzanych katalogów.

## [0.1.1] - 2026-08-10

### Added

- Governance repozytorium: CODEOWNERS, formularze zgloszen, checklista pull requestow i polityka bezpieczenstwa.
- CycloneDX SBOM, sumy SHA-256 i GitHub Artifact Attestation dla nowych wydan.
- Analiza CodeQL dla kodu Pythona na `main`, w pull requestach i co tydzien.
- Kontrakty odpowiedzi API z correlation ID oraz testy poprawnego zamykania serwera.
- Testy wlasnosciowe Hypothesis dla normalizacji, wyszukiwania i serializacji danych.
- Scenariusze E2E interfejsu w Chromium przez Playwright, uruchamiane w osobnym zadaniu CI.
- Podniesienie wymaganej wartosci coverage z 90% do 93%.
- Wersjonowany kontrakt OpenAPI 3.1 oraz osobne endpointy liveness i readiness.
- Metryki Prometheus dla requestow, bledow, fallbackow, przeciazenia i czasu odpowiedzi.
- Laczny deadline pobierania NOAA z kontrolowana odpowiedzia `504 NOAA_TIMEOUT`.
- Naglowki CSP, `X-Content-Type-Options`, `Referrer-Policy` i `Permissions-Policy` dla HTTP.

### Security

- Przypiecie wszystkich zewnetrznych GitHub Actions do pelnych commit SHA.
- Wlaczenie GitHub Secret Scanning z push protection i aktualizacji bezpieczenstwa Dependabota.
- Blokada SSRF przez wymuszenie publicznych adresow HTTPS i walidacje kazdego przekierowania.
- Ograniczenie naglowkow z tokenem NOAA wylacznie do hostow w domenie `noaa.gov`.
- Ograniczenie plikow cache wskazywanych przez GUI do dedykowanego katalogu aplikacji.
- Testy kompatybilnosci workflow wydania instaluja zaleznosci Hypothesis i OpenAPI oraz pomijaja osobny zestaw E2E.

## [0.1.0] - 2026-08-09

### Added

- CLI do wyszukiwania, filtrowania, opisu i eksportu stacji NOAA.
- Bootstrap GUI z wyszukiwaniem po kraju, miescie i identyfikatorze stacji.
- Cache danych z polityka fresh/stale, fallbackiem i metadanymi zrodla.
- Rotacja tokenow NOAA z cooldownem, kwarantanna oraz retry z backoffem.
- Strukturalne logi JSON, correlation ID i bezpieczne odpowiedzi bledow HTTP.
- Limit czterech rownoleglych fetchy z kontrolowana odpowiedzia `503 SERVER_BUSY`.
- Testy Python 3.10, 3.12 i 3.14 oraz budowanie wheel i sdist w CI.
- Skanowanie Ruff, mypy, Bandit, pip-audit i prog coverage 90%.
- Zaplanowany i reczny smoke test rzeczywistego API NOAA.

### Security

- Ograniczenie rozmiaru request body do 256 KiB.
- Ograniczenie zdalnych URL-i GUI do HTTPS w domenie `noaa.gov`.
- Renderowanie danych tabeli przez `textContent` zamiast `innerHTML`.
- Tokeny NOAA i szczegoly wyjatkow nie sa zwracane klientowi ani logowane.

[Unreleased]: https://github.com/S3bx0/Dane-Meteo-Stacje/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/S3bx0/Dane-Meteo-Stacje/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/S3bx0/Dane-Meteo-Stacje/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/S3bx0/Dane-Meteo-Stacje/releases/tag/v0.1.0
