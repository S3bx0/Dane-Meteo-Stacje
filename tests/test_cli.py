import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_search_by_city_name_returns_station_matches():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "Bialystok"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "bialystok" in result.stdout.lower()
    assert "PLM00012295" in result.stdout


def test_info_for_known_station_shows_metadata():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "info", "PLM00012295"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Bialystok" in result.stdout
    assert "PLM00012295" in result.stdout


def test_search_can_output_json():
    result = subprocess.run(
        [sys.executable, "-m", "dane_meteo_stacje", "search", "Bialystok", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert '"station_id": "PLM00012295"' in result.stdout
