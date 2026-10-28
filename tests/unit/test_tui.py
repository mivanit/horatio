from __future__ import annotations


def test_tui_unit() -> None:
    """tui.py: terminal detection, escape-safe clipping, URL building; server colour depths."""
    import server
    import tui

    tty: dict[str, str] = {"TERM": "xterm-256color"}
    assert tui.detect(tty, True, "UTF-8") == ("unicode", "256"), (
        "xterm-256color + UTF-8 -> unicode, 256"
    )
    assert tui.detect({**tty, "COLORTERM": "truecolor"}, True, "utf-8")[1] == "on", (
        "COLORTERM truecolor -> 24-bit"
    )
    assert (
        tui.detect({**tty, "NO_COLOR": "1", "COLORTERM": "truecolor"}, True, "UTF-8")[1]
        == "off"
    ), "NO_COLOR wins"
    assert tui.detect({**tty, "FORCE_COLOR": "1"}, True, "UTF-8")[1] == "16", (
        "FORCE_COLOR=1 -> 16"
    )
    assert tui.detect({"TERM": "linux"}, True, "UTF-8") == ("ascii", "16"), (
        "Linux console -> ascii, 16"
    )
    assert tui.detect({"TERM": "dumb"}, True, "ANSI_X3.4-1968") == ("ascii", "off"), (
        "dumb + C locale -> ascii, off"
    )
    assert tui.detect(tty, False, "UTF-8") == ("simple", "off"), "not a tty -> simple"
    red: str = "\033[31mabcdef\033[0m"
    assert tui.clip(red, 3) == "\x1b[31mabc\x1b[0m", (
        f"clip keeps escapes whole: {repr(tui.clip(red, 3))}"
    )
    assert tui.clip("abc", 10) == "abc", "clip leaves short lines alone"
    url: str = tui.build_url(
        "http://h:8080/", {"view": "both", "hours": None, "width": 120}
    )
    assert url == "http://h:8080/text?view=both&width=120", (
        f"build_url drops unset params: {url}"
    )
    assert server._paint("x", "cpu", "256").startswith("\x1b[38;5;"), "256-colour depth"
    assert server._paint("x", "muted", "256") == "\x1b[38;5;244mx\x1b[0m", (
        "greys use the 256 grey ramp"
    )
    assert server._paint("x", "error", "16").startswith("\x1b[3") or server._paint(
        "x", "error", "16"
    ).startswith("\x1b[9"), "16-colour depth"
    tall: str = "\n".join(str(i) for i in range(50)) + "\n"
    fit: str = server._fit_height(tall, 10, False)
    assert fit.count("\n") == 10 and "41 more lines" in fit, (
        f"height fit trims and says how much: {repr(fit[-40:])}"
    )
