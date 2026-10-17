#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""Live terminal view of a horatio server: the /text report, redrawn in place.

Standard library only, so it runs with no install:

    curl -s http://<host>:8080/tui.py | python3 - http://<host>:8080
    python3 tui.py http://<host>:8080          # from a horatio checkout
    python3 tui.py                             # this box (localhost:8080)

The server renders; this script picks the width, height, colour depth and
character set for the terminal, polls, and redraws. Keys: q quit, r refresh,
v cycle view (both / snapshot / history), t table, + / - history window,
? help. Not a tty (piped or redirected): prints one plain frame and exits.
"""

from __future__ import annotations

import argparse
import locale
import os
import re
import select
import shutil
import signal
import sys
import time
import urllib.parse
import urllib.request

VIEWS = ("both", "latest", "history")
HOURS_STEPS = (0.5, 1, 2, 3, 6, 12, 24, 48, 168)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def detect(env: dict, isatty: bool, encoding: str) -> tuple[str, str]:
    """(style, color) for /text from the environment; pure, so it's testable.

    Colour: NO_COLOR > FORCE_COLOR > COLORTERM truecolor > TERM *256color >
    TERM dumb/empty > 16 colours. Unicode needs a UTF-8 locale and not the
    Linux console (its font lacks the block characters).
    """
    term = env.get("TERM", "").lower()
    if not isatty:
        return "simple", "off"
    if env.get("NO_COLOR"):
        color = "off"
    elif "FORCE_COLOR" in env:
        color = {"0": "off", "1": "16", "2": "256"}.get(env["FORCE_COLOR"], "on")
    elif env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        color = "on"
    elif "256color" in term:
        color = "256"
    elif term in ("", "dumb"):
        color = "off"
    else:
        color = "16"
    utf8 = encoding.lower().replace("-", "") in ("utf8",)
    style = "unicode" if utf8 and term != "linux" else "ascii"
    return style, color


def _has_tty() -> bool:
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY))
        return True
    except OSError:
        return False


def build_url(base: str, params: dict) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"{base.rstrip('/')}/text?{query}"


def fetch(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "horatio-tui"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def clip(line: str, width: int) -> str:
    """Cut a line to `width` visible columns without splitting an escape."""
    out, seen, pos = [], 0, 0
    for m in ANSI.finditer(line):
        text = line[pos : m.start()]
        if seen + len(text) >= width:
            out.append(text[: width - seen])
            return "".join(out) + "\x1b[0m"
        out.append(text)
        seen += len(text)
        out.append(m.group())
        pos = m.end()
    out.append(line[pos:][: max(0, width - seen)])
    return "".join(out)


class Terminal:
    """Alternate screen, hidden cursor and cbreak input; restored on exit.

    Keys come from /dev/tty, not stdin: under `curl ... | python3 -` stdin is
    the pipe the script itself arrived on."""

    def __enter__(self) -> Terminal:
        import termios
        import tty

        self.fd = os.open("/dev/tty", os.O_RDONLY)
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        os.close(self.fd)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def key(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        return os.read(self.fd, 1).decode(errors="ignore") if ready else None


def draw(frame: str, status: str, cols: int, rows: int) -> None:
    lines = frame.rstrip("\n").split("\n")[: rows - 1]
    out = ["\x1b[?2026h\x1b[H"]  # synchronized update (ignored where unsupported), home
    out += [clip(line, cols) + "\x1b[K\n" for line in lines]
    out.append("\x1b[J")
    out.append(f"\x1b[{rows};1H\x1b[7m{clip(status, cols).ljust(cols)}\x1b[0m\x1b[?2026l")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", nargs="?", default="http://localhost:8080",
                        help="horatio server (default http://localhost:8080)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes (min 1)")
    parser.add_argument("--color", choices=("on", "256", "16", "off"), help="override colour detection")
    parser.add_argument("--style", choices=("unicode", "ascii", "simple"), help="override character set")
    parser.add_argument("--hours", type=float, help="history window (default: the server's 1-3 h)")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    args = parser.parse_args()

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    isatty = sys.stdout.isatty() and _has_tty()
    encoding = sys.stdout.encoding or locale.getpreferredencoding(False)
    style, color = detect(dict(os.environ), isatty, encoding)
    style, color = args.style or style, args.color or color
    interval = max(1.0, args.interval)
    state = {"view": 0, "table": False, "hours": args.hours}

    def url(cols: int, rows: int | None) -> str:
        return build_url(args.url, {
            "view": VIEWS[state["view"]], "style": style, "color": color,
            "width": max(60, cols), "height": None if rows is None else max(5, rows - 1),
            "hours": state["hours"], "table": 1 if state["table"] else None,
        })

    if args.once or not isatty:
        cols, _ = shutil.get_terminal_size((100, 50))
        try:
            sys.stdout.write(fetch(url(cols, None)))
        except OSError as e:
            print(f"horatio-tui: {args.url}: {e}", file=sys.stderr)
            return 1
        return 0

    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    frame, error, fetched, backoff = "", "", 0.0, interval
    show_help = False
    try:
        with Terminal() as term:
            next_fetch = 0.0
            while True:
                cols, rows = shutil.get_terminal_size()
                now = time.monotonic()
                if resized[0] or now >= next_fetch:
                    resized[0] = False
                    try:
                        frame, error = fetch(url(cols, rows)), ""
                        fetched, backoff = now, interval
                    except OSError as e:
                        error, backoff = str(e), min(10.0, backoff * 2)
                    next_fetch = now + backoff
                age = f"{time.monotonic() - fetched:.0f}s ago" if fetched else "never"
                if show_help:
                    status = " q quit  r refresh  v view  t table  +/- history window  ? close help"
                else:
                    hours = f"{state['hours']:g}h" if state["hours"] else "auto window"
                    status = (f" {args.url}  {VIEWS[state['view']]}  {hours}  {color}/{style}"
                              f"  updated {age}  {'ERROR: ' + error if error else ''}  ? keys")
                draw(frame or "connecting...", status, cols, rows)
                k = term.key(min(0.5, max(0.0, next_fetch - time.monotonic())))
                if not k:
                    continue
                if k in ("q", "Q", "\x03"):
                    return 0
                if k == "?":
                    show_help = not show_help
                    continue
                if k == "v":
                    state["view"] = (state["view"] + 1) % len(VIEWS)
                elif k == "t":
                    state["table"] = not state["table"]
                elif k in "+=-_":
                    cur = state["hours"] or 2
                    i = min(range(len(HOURS_STEPS)), key=lambda j: abs(HOURS_STEPS[j] - cur))
                    i = min(len(HOURS_STEPS) - 1, i + 1) if k in "+=" else max(0, i - 1)
                    state["hours"] = HOURS_STEPS[i]
                elif k != "r":
                    continue
                next_fetch = 0.0  # refetch now with the new settings
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
