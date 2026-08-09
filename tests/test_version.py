from importlib.metadata import version

import dane_meteo_stacje


def test_runtime_version_matches_installed_package_metadata():
    assert dane_meteo_stacje.__version__ == version("dane-meteo-stacje")