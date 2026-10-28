from __future__ import annotations

import os
import shutil

import pytest


@pytest.fixture
def node() -> str:
    command: str | None = shutil.which("node")
    if command is None:
        if os.environ.get("HORATIO_SKIP_NODE") == "1":
            pytest.skip("Node.js is unavailable (HORATIO_SKIP_NODE=1)")
        pytest.fail("Node.js 18+ is required; install it or set HORATIO_SKIP_NODE=1")
    return command
