"""Load fresh application modules from disposable files for each unit test."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def isolated_app(app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name: str
    for name in ("server", "tui"):
        spec: importlib.machinery.ModuleSpec | None = (
            importlib.util.spec_from_file_location(name, app_dir / f"{name}.py")
        )
        assert spec is not None and spec.loader is not None
        module: ModuleType = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
