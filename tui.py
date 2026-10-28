#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
#
# ============================================================================
#  horatio live terminal view
#
#  You're reading the script itself. To run it, pipe it into Python instead:
#
#      curl -fsS http://<host>:8080/tui.py | python3 -
#
#  or save it and run it (read it first if you don't trust the network):
#
#      curl -fsS -o tui.py http://<host>:8080/tui.py && python3 tui.py
#
#  On the box itself, from the horatio checkout:   python3 tui.py
#  Needs Python 3.8+ on Linux or macOS (on Windows, use WSL). Nothing to install.
#  Keys are shown at the bottom of the screen; q quits.
# ============================================================================
"""Live terminal view of a horatio server: the /text report, redrawn in place.

Standard library only, so it runs with no install:

    curl -fsS http://<host>:8080/tui.py | python3 -   # knows its server
    python3 tui.py http://<host>:8080                 # from a horatio checkout
    python3 tui.py                                    # this box's horatio

The server renders; this script picks the width, height, colour depth and
character set for the terminal, polls, and redraws. Keys: q quit, r refresh,
v cycle view (both / snapshot / history), t table, + / - history window,
listed at the bottom like nano. Not a tty (piped or redirected): prints one
plain frame and exits.
"""

from __future__ import annotations

import argparse
import http.client
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

# Filled in by the server when it serves this file (GET /tui.py), so a
# downloaded copy already knows where it came from.
SERVED_URL = ""
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


def default_url(unit: str = "/etc/systemd/system/horatio.service") -> str:
    """Where to connect when no URL is given: $HORATIO_URL, else the server
    this copy was downloaded from, else this box's horatio (port from its
    systemd unit's --port, default 8080)."""
    if os.environ.get("HORATIO_URL"):
        return os.environ["HORATIO_URL"]
    if SERVED_URL:
        return SERVED_URL
    port = 8080
    try:
        with open(unit) as f:
            m = re.search(r"--port[= ](\d+)", f.read())
        if m:
            port = int(m.group(1))
    except OSError:
        pass
    return f"http://localhost:{port}"


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


# The bottom bar, nano style: key in reverse video, then what it does.
KEYS = (
    ("q", "Quit"),
    ("r", "Refresh"),
    ("v", "View"),
    ("t", "Table"),
    ("+", "Longer"),
    ("-", "Shorter"),
)
BAR_ROWS = 2  # status line + key list


def key_bar(cols: int, right: str = "") -> str:
    """`q Quit  r Refresh ...` with each key highlighted, cut to fit, and
    `right` (the server) right-aligned when there's room."""
    out, used = [], 0
    for key, label in KEYS:
        cell = f" {key} {label} "
        if used + len(cell) > cols:
            break
        out.append(f"\x1b[7m {key} \x1b[0m{label} ")
        used += len(cell)
    if right and used + len(right) + 2 <= cols:
        out.append(" " * (cols - used - len(right) - 1) + f"\x1b[2m{right}\x1b[0m")
    return "".join(out)


def draw(frame: str, status: str, cols: int, rows: int, host: str = "") -> None:
    lines = frame.rstrip("\n").split("\n")[: rows - BAR_ROWS]
    out = ["\x1b[?2026h\x1b[H"]  # synchronized update (ignored where unsupported), home
    out += [clip(line, cols) + "\x1b[K\n" for line in lines]
    out.append("\x1b[J")
    out.append(f"\x1b[{rows - 1};1H\x1b[7m{clip(status, cols).ljust(cols)}\x1b[0m")
    out.append(f"\x1b[{rows};1H{key_bar(cols, host)}\x1b[K\x1b[?2026l")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="horatio server (default: $HORATIO_URL, the server this was "
        "downloaded from, or this box's horatio)",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="seconds between refreshes (min 1)"
    )
    parser.add_argument(
        "--color", choices=("on", "256", "16", "off"), help="override colour detection"
    )
    parser.add_argument(
        "--style", choices=("unicode", "ascii", "simple"), help="override character set"
    )
    parser.add_argument(
        "--hours", type=float, help="history window (default: the server's 1-3 h)"
    )
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    args = parser.parse_args()
    args.url = args.url or default_url()

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
        return build_url(
            args.url,
            {
                "view": VIEWS[state["view"]],
                "style": style,
                "color": color,
                "width": max(60, cols),
                "height": None if rows is None else max(5, rows - BAR_ROWS),
                "hours": state["hours"],
                "table": 1 if state["table"] else None,
            },
        )

    if args.once or not isatty:
        cols, _ = shutil.get_terminal_size((100, 50))
        try:
            sys.stdout.write(fetch(url(cols, None)))
        except (OSError, http.client.HTTPException) as e:
            print(f"horatio-tui: {args.url}: {e}", file=sys.stderr)
            return 1
        return 0

    host = urllib.parse.urlsplit(args.url).netloc or args.url
    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    frame, error, fetched, backoff = "", "", 0.0, interval
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
                    except (OSError, http.client.HTTPException) as e:
                        error, backoff = str(e), min(10.0, backoff * 2)
                    next_fetch = now + backoff
                age = f"{time.monotonic() - fetched:.0f}s ago" if fetched else "never"
                hours = f"{state['hours']:g}h" if state["hours"] else "auto window"
                status = (
                    f" {VIEWS[state['view']]}  {hours}  {color}/{style}"
                    f"  updated {age}  {'ERROR: ' + error if error else ''}"
                )
                draw(frame or f"connecting to {args.url} ...", status, cols, rows, host)
                k = term.key(min(0.5, max(0.0, next_fetch - time.monotonic())))
                if not k:
                    continue
                if k in ("q", "Q", "\x03"):
                    return 0
                if k == "v":
                    state["view"] = (state["view"] + 1) % len(VIEWS)
                elif k == "t":
                    state["table"] = not state["table"]
                elif k in "+=-_":
                    cur = state["hours"] or 2
                    i = min(
                        range(len(HOURS_STEPS)), key=lambda j: abs(HOURS_STEPS[j] - cur)
                    )
                    i = min(len(HOURS_STEPS) - 1, i + 1) if k in "+=" else max(0, i - 1)
                    state["hours"] = HOURS_STEPS[i]
                elif k != "r":
                    continue
                next_fetch = 0.0  # refetch now with the new settings
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

# ============================================================================
#  End of the horatio terminal view. To run it rather than read it:
#
#      curl -fsS http://<host>:8080/tui.py | python3 -
#
#  Options: python3 tui.py --help   (--interval, --color, --style, --hours,
#  --once). Plain one-off report without this script:
#
#      curl "http://<host>:8080/text?width=$COLUMNS"
#
#  Full guide: http://<host>:8080/help
# ============================================================================
