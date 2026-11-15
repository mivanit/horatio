"""VERSION is the one release number (README: Versions and updates)."""

from __future__ import annotations

import re

from tests.support import ROOT


def test_version_is_one_semver_line() -> None:
    text: str = (ROOT / "VERSION").read_text()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\n", text), text
