"""Native Lua/Nixio checks, selected explicitly with make test-openwrt."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.support import ROOT


@pytest.fixture(scope="module")
def lua() -> str:
    command: str | None = shutil.which(os.environ.get("LUA", "lua5.1"))
    if command is None:
        pytest.fail(
            "Native OpenWrt tests need Lua 5.1 and Nixio. "
            "See README.md's OpenWrt test setup, or use make check for the Linux suite."
        )
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            command,
            "-e",
            'assert(_VERSION == "Lua 5.1", "Lua 5.1 is required"); require("nixio"); '
            'assert(os.date("%z", 1704067200) == "-0500" and os.date("%z", 1719792000) == "-0400", '
            '"America/New_York timezone data is required")',
        ],
        env=dict(os.environ, TZ="America/New_York"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Lua/Nixio prerequisites failed:\n{result.stderr}"
    return command


@pytest.mark.parametrize(
    "script,timezone",
    [
        ("handler", "UTC"),
        ("metrics", "UTC"),
        ("history", "UTC"),
        ("history", "America/New_York"),
    ],
)
def test_native(
    lua: str,
    script: Literal["handler", "metrics", "history"],
    timezone: Literal["UTC", "America/New_York"],
) -> None:
    path: Path = Path(__file__).with_name(f"test_{script}.lua")
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [lua, str(path)],
        cwd=ROOT,
        env=dict(os.environ, TZ=timezone),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
