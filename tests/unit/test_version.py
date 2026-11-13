"""VERSION is the one release number (README: Versions and updates); the copies
that tools read elsewhere must agree with it."""

from __future__ import annotations

import re

from tests.support import ROOT

SEMVER: str = r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?"


def version() -> str:
    return (ROOT / "VERSION").read_text().strip()


def test_version_is_semver() -> None:
    assert re.fullmatch(SEMVER, version()), version()
    assert (ROOT / "VERSION").read_text() == version() + "\n", "one line, no padding"


def test_pyproject_and_lock_match() -> None:
    pyproject: str = (ROOT / "pyproject.toml").read_text()
    match: re.Match[str] | None = re.search(r'^version = "([^"]+)"$', pyproject, re.M)
    assert match is not None and match.group(1) == version()
    lock: str = (ROOT / "uv.lock").read_text()
    assert f'name = "horatio"\nversion = "{version()}"\n' in lock


def test_changelog_has_the_release() -> None:
    changelog: str = (ROOT / "CHANGELOG.md").read_text()
    assert "\n## [Unreleased]\n" in changelog
    assert re.search(
        rf"^## \[{re.escape(version())}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M
    ), f"CHANGELOG.md has no dated section for {version()}"
