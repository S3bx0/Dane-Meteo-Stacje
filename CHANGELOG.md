# Changelog

Wszystkie istotne zmiany w projekcie sa dokumentowane w tym pliku.
Format jest oparty na Keep a Changelog, a wersje stosuja Semantic Versioning.

## [Unreleased]

### Added

- CycloneDX SBOM, sumy SHA-256 i GitHub Artifact Attestation dla nowych wydan.
- Analiza CodeQL dla kodu Pythona na `main`, w pull requestach i co tydzien.
- Kontrakty odpowiedzi API z correlation ID oraz testy poprawnego zamykania serwera.
- Testy wlasnosciowe Hypothesis dla normalizacji, wyszukiwania i serializacji danych.
- Scenariusze E2E interfejsu w Chromium przez Playwright, uruchamiane w osobnym zadaniu CI.
- Podniesienie wymaganej wartosci coverage z 90% do 93%.

### Security

- Przypiecie wszystkich zewnetrznych GitHub Actions do pelnych commit SHA.
- Wlaczenie GitHub Secret Scanning z push protection i aktualizacji bezpieczenstwa Dependabota.
- Blokada SSRF przez wymuszenie publicznych adresow HTTPS i walidacje kazdego przekierowania.
- Ograniczenie naglowkow z tokenem NOAA wylacznie do hostow w domenie `noaa.gov`.
- Ograniczenie plikow cache wskazywanych przez GUI do dedykowanego katalogu aplikacji.

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

[Unreleased]: https://github.com/S3bx0/Dane-Meteo-Stacje/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/S3bx0/Dane-Meteo-Stacje/releases/tag/v0.1.0