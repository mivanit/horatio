from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any


def test_text_view_unit() -> None:
    """server.render_text: ascii stays 7-bit, unicode draws bars, empty and gappy input."""
    import server

    now: datetime = datetime.now(timezone.utc)
    recs: list[dict[str, Any]] = [
        {
            "timestamp": (now - timedelta(minutes=10 * (3 - k))).isoformat(),
            "cpu": {"avg": 10.0 * k, "max": 50.0},
            "memory": {"used_gb": 4.0, "total_gb": 16.0, "percent": 25.0},
            "gpus": [{"index": 0, "util_percent": 80.0, "mem_used_gb": None}],
            "temps": {"GPU 0": 60.0, "Tctl": 45.0},
            "ping": {"8.8.8.8": {"avg": None, "loss": 1.0}},
        }
        for k in range(4)
    ]
    recs[1] = {"timestamp": recs[1]["timestamp"]}  # a record missing every metric
    ascii_out: str = server.render_text(recs, style="ascii", title="box")
    assert ascii_out.isascii(), "ascii output is 7-bit"
    assert "GPU 0" in ascii_out and "  memory" in ascii_out and ("DOWN" in ascii_out), (
        "latest shows GPU util + memory and down ping"
    )
    assert ascii_out.count("\n") > 10 and "History" in ascii_out, (
        "history table rendered"
    )
    uni: str = server.render_text(recs, style="unicode", view="latest", title="box")
    assert "█" in uni and "History" not in uni, (
        "unicode latest-only has bars, no history"
    )
    hist: str = server.render_text(
        recs, style="ascii", view="history", rows=2, table=True
    )
    assert "Snapshot" not in hist and hist.count(" | ") == 3, (
        f"history-only, rows capped: {repr(hist.count(' | '))}"
    )
    newest_time: datetime | None = server._record_time(recs[-1])
    assert newest_time is not None
    newest_stamp: str = newest_time.strftime("%m-%d %H:%M:%S")
    one: str = server.render_text(
        recs, style="ascii", view="history", rows=1, table=True
    )
    one_rows: list[str] = [line for line in one.splitlines() if " | " in line][1:]
    assert len(one_rows) == 1 and one_rows[0].startswith(newest_stamp), (
        "a one-row history table shows the newest record"
    )
    roomy: str = server.render_text(
        recs, style="ascii", view="history", rows=24, table=True
    )
    roomy_rows: list[str] = [line for line in roomy.splitlines() if " | " in line][1:]
    assert len(roomy_rows) == len(recs) and roomy_rows[-1].startswith(newest_stamp), (
        "a history table with spare rows includes every record"
    )
    assert "No history records" in server.render_text([]), "empty history"
    colored: str = server.render_text(recs, style="unicode", color=True)
    assert "\x1b[38;2;59;130;246m" in colored, "colour mode uses the frontend CPU blue"
    assert "\x1b[" not in uni, "no escapes unless colour is on"
    assert " | " not in server.render_text(recs, style="ascii"), (
        "no table unless table=1"
    )
    wide: str = server.render_text(recs, width=180)
    assert max((len(server._strip_ansi(x)) for x in wide.splitlines())) <= 180 and any(
        ("Snapshot" in x and "History" in x for x in wide.splitlines())
    ), "width=180 puts snapshot and history side by side within the width"
    narrow: str = server.render_text(recs, width=80)
    assert max((len(x) for x in narrow.splitlines())) <= 80, (
        "width=80 stays within 80 columns"
    )
    assert server._sparkline([72.2, 74.0], lo=0, hi=100) == "▆▆", (
        "percent rows use a fixed 0-100 scale"
    )
    assert server._sparkline([0.0, 0.0], lo=0, hi=100) != server._sparkline(
        [100.0, 100.0], lo=0, hi=100
    ), "flat 0% and flat 100% look different"
    assert (
        server.auto_history_hours(80, "both", True, 600) == 3.0
        and server.auto_history_hours(80, "both", True, 60) == 1.0
        and (1.0 < server.auto_history_hours(80, "both", True, 120) < 3.0)
    ), "auto history window stays in 1-3 h and scales with the record interval"
    # Use the host's local timezone just as the real /text route does. The
    # timezone attached by astimezone() has only the offset at that instant.
    old_tz: str | None = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        utc_end: datetime
        expected_start: str
        for utc_end, expected_start in [
            (datetime(2026, 3, 8, 16, tzinfo=timezone.utc), "23:00"),
            (datetime(2026, 11, 1, 17, tzinfo=timezone.utc), "01:00"),
        ]:
            foot: list[str]
            _, _, foot, _ = server._render_history(
                recs,
                False,
                12,
                24,
                False,
                now=utc_end.astimezone(),
            )
            ticks: str = foot[1].strip()
            assert ticks.startswith(expected_start) and ticks.endswith("12:00"), (
                f"history time axis crosses DST correctly ({utc_end:%Y-%m-%d}): {ticks}"
            )
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
