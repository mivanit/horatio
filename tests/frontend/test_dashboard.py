"""Run the browser regression fixtures with Node's built-in test dependencies."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_dashboard(node: str) -> None:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [node, str(Path(__file__).with_name("dashboard.js"))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
