"""Disposable application files for both pytest and Makefile test runs."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.support import ROOT


def copy_application(destination: Path) -> Path:
    """Copy application code and example config, never the checkout's live state."""
    destination.mkdir(parents=True)
    name: str
    for name in ("server.py", "tui.py", "index.html"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(ROOT / "static", destination / "static")
    shutil.copy2(ROOT / "config.example.json", destination / "config.json")
    return destination


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    return copy_application(tmp_path / "app")


@pytest.fixture(scope="session")
def live_app_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_application(tmp_path_factory.mktemp("live-server") / "app")
