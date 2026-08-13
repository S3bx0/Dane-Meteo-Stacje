from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_private_user_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Tests must never read a developer's real per-user NOAA token file."""

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
