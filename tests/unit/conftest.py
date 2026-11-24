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
    # hwmon first: server imports it, and must get the copy, not the checkout's.
    for name in ("hwmon", "hostcheck", "zfs", "server", "tui"):
        spec: importlib.machinery.ModuleSpec | None = (
            importlib.util.spec_from_file_location(
                name, app_dir / "python" / f"{name}.py"
            )
        )
        assert spec is not None and spec.loader is not None
        module: ModuleType = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    # The host checks read this machine's /run and NVIDIA driver; tests that want
    # them call hostcheck's functions with a fake tree instead.
    hostcheck: ModuleType = sys.modules["hostcheck"]
    monkeypatch.setattr(hostcheck, "host_findings", hostcheck.Findings)
    # Likewise zpool and lsblk; tests/unit/test_zfs.py calls zfs.zfs_findings itself.
    monkeypatch.setattr(
        sys.modules["server"],
        "get_zfs",
        lambda: (hostcheck.Findings(), {"status": "absent", "pools": []}),
    )
