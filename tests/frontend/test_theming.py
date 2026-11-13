"""Every color comes from a style.css variable, so a theme (themes/*.css) that
overrides the variables restyles the whole page."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import ROOT

STYLE: Path = ROOT / "static" / "style.css"
# Set inline by app.js at runtime, not in :root.
RUNTIME_VARS: frozenset[str] = frozenset({"--grid-columns"})
COLOR_LITERAL: re.Pattern[str] = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(", re.I
)


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def root_and_rest(css: str) -> tuple[str, str]:
    """The first :root block's body, and everything after it."""
    match: re.Match[str] | None = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert match is not None, "no :root block"
    return match.group(1), css[match.end() :]


def defined_vars() -> set[str]:
    root, _ = root_and_rest(strip_comments(STYLE.read_text()))
    return set(re.findall(r"(--[\w-]+)\s*:", root))


def theme_files() -> list[Path]:
    return sorted((ROOT / "themes").glob("*.css"))


def test_no_color_literals_outside_root() -> None:
    _, rest = root_and_rest(strip_comments(STYLE.read_text()))
    found: list[str] = [
        line.strip() for line in rest.splitlines() if COLOR_LITERAL.search(line)
    ]
    assert not found, f"use a :root variable instead: {found}"


def test_used_vars_are_defined() -> None:
    defined: set[str] = defined_vars()
    assert {"--bg-body", "--font-family", "--chart-font-size"} <= defined
    sources: list[Path] = [STYLE, *theme_files()]
    undefined: dict[str, set[str]] = {}
    path: Path
    for path in sources:
        used: set[str] = set(re.findall(r"var\(\s*(--[\w-]+)", path.read_text()))
        # A theme may use variables it defines itself (classic's bevels).
        own: set[str] = set(re.findall(r"(--[\w-]+)\s*:", path.read_text()))
        missing: set[str] = used - defined - own - RUNTIME_VARS
        if missing:
            undefined[path.name] = missing
    for path in sorted((ROOT / "static").glob("*.js")):
        if path.name == "chart.umd.min.js":
            continue
        read: set[str] = set(
            re.findall(r"getCss(?:Color|Number)\('(--[\w-]+)'\)", path.read_text())
        )
        if read - defined:
            undefined[path.name] = read - defined
    assert not undefined, undefined


def test_themes_override_known_vars() -> None:
    """A misspelled variable in a theme silently does nothing."""
    defined: set[str] = defined_vars()
    assert {p.stem for p in theme_files()} >= {"light", "classic"}
    path: Path
    for path in theme_files():
        root, _ = root_and_rest(strip_comments(path.read_text()))
        unknown: set[str] = {
            v
            for v in re.findall(r"(--[\w-]+)\s*:", root)
            if v not in defined and not v.startswith("--bevel-")
        }
        assert not unknown, f"{path.name}: {unknown}"


def test_no_hex_alpha_concatenation() -> None:
    """`color + '20'` breaks on a theme color that isn't #rrggbb; use withAlpha."""
    path: Path
    for path in sorted((ROOT / "static").glob("*.js")):
        if path.name == "chart.umd.min.js":
            continue
        hits: list[str] = re.findall(r"\+\s*'[0-9a-fA-F]{2}'", path.read_text())
        assert not hits, f"{path.name}: {hits}"
