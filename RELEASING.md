# Wydawanie wersji

GitHub Release jest tworzony automatycznie dopiero po wypchnieciu taga `vX.Y.Z`.
Workflow nie publikuje pakietu do PyPI.

## Przygotowanie

1. Upewnij sie, ze `main` jest czysty i zsynchronizowany z `origin/main`.
2. Zaktualizuj `dane_meteo_stacje.__version__` zgodnie z Semantic Versioning.
3. Przenies zmiany z sekcji `Unreleased` w `CHANGELOG.md` do nowej wersji i daty.
4. Uruchom lokalnie Ruff, mypy, Bandit, pip-audit oraz pytest z coverage 90%.
5. Zbuduj paczki przez `python -m build` i sprawdz je przez `python -m twine check dist/*`.
6. Commituj przygotowanie wydania i poczekaj na zielone CI na `main`.

## Utworzenie wydania

Utworzenie i wypchniecie taga jest operacja publikujaca:

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

Workflow `Release`:

- porownuje tag z `dane_meteo_stacje.__version__`,
- uruchamia testy na Pythonie 3.10, 3.12 i 3.14,
- buduje oraz sprawdza wheel i sdist,
- instaluje wheel w czystym srodowisku,
- generuje CycloneDX SBOM i plik `SHA256SUMS`,
- tworzy GitHub Artifact Attestation dla artefaktow,
- tworzy GitHub Release i dolacza pliki dystrybucji, SBOM oraz sumy kontrolne.

Po zakonczeniu workflow sprawdz attestation poleceniem:

```bash
VERSION=0.1.1
gh attestation verify "dane_meteo_stacje-${VERSION}-py3-none-any.whl" \
	--repo S3bx0/Dane-Meteo-Stacje
```

Nie wypychaj taga ponownie po nieudanym wydaniu. Popraw przyczyne, zwieksz wersje i utworz
nowy tag. Publikacja do PyPI wymaga osobnej decyzji oraz konfiguracji trusted publishing.