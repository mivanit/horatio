"""Alarm history ranges preserve recorded events without inferring health."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

STAMP = 1_790_000_000.0


def event(at: float, kind: str = "raised", **more: Any) -> dict[str, Any]:
    import server

    return {
        "timestamp": server._iso(at),
        "source": "state",
        "event": kind,
        "key": "journal/test",
        "text": "test fault",
        "since": STAMP - 1000,
        **more,
    }


def test_history_filters_by_time_not_append_order_and_keeps_terminal_context(
    tmp_path: Path,
) -> None:
    import server

    rows = [
        event(STAMP + 20, "cleared", active_for_s=1020),
        event(STAMP - 20),
        event(STAMP + 10),
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    data = server._read_alarm_history(STAMP, STAMP + 30, path)
    assert data["events"] == [rows[2], rows[0]]
    assert data["events"][1]["since"] < data["cutoff"]
    assert not data["truncated"] and data["skipped"] == 0


def test_history_has_no_fifty_event_limit_and_keeps_occurrences_distinct(
    tmp_path: Path,
) -> None:
    import server

    rows = [event(STAMP, "cleared"), event(STAMP + 1, since=STAMP + 1)]
    rows += [
        event(STAMP + i, "changed", since=STAMP + 1, value=i) for i in range(2, 70)
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert server._read_alarm_history(STAMP, STAMP + 100, path)["events"] == rows


def test_history_bounds_bytes_skips_torn_lines_and_reports_incomplete_data(
    tmp_path: Path,
) -> None:
    import server

    path = tmp_path / "events.jsonl"
    good = json.dumps(event(STAMP)) + "\n"
    path.write_text("prefix too large to inspect\n" + good + "bad json\n" + '{"event":')
    data = server._read_alarm_history(
        STAMP - 1, STAMP + 1, path, max_bytes=len(good) + 25
    )
    assert data["events"] == [event(STAMP)]
    assert data["truncated"] and data["skipped"] == 2


def test_history_rejects_invalid_dates_and_preserves_equal_time_order(
    tmp_path: Path,
) -> None:
    import server

    rows = [event(STAMP, "raised"), event(STAMP, "retired", note="replaced fan")]
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                event(STAMP, timestamp="bad"),
                event(STAMP, timestamp="2026-10-23T12:00:00.5Z"),
                *rows,
            ]
        )
    )
    data = server._read_alarm_history(STAMP, STAMP + 1, path)
    assert data["events"] == rows
    assert data["skipped"] == 1  # valid fractional timestamp is just outside the range


def test_history_read_failure_is_not_empty_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import server

    assert (
        server._read_alarm_history(STAMP, STAMP + 1, tmp_path / "absent")["events"]
        == []
    )

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("unreadable")

    monkeypatch.setattr(server, "_read_alarm_history", failed)
    response = asyncio.run(server.alarm_history())
    assert response.status_code == 503


def test_history_read_and_encoding_leave_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    thread = threading.get_ident()
    calls = []

    def read(cutoff: float, now: float) -> dict[str, Any]:
        assert threading.get_ident() != thread
        calls.append((cutoff, now))
        return {"events": []}

    original = server.json.dumps

    def encode(value: Any, **kwargs: Any) -> str:
        assert threading.get_ident() != thread
        return original(value, **kwargs)

    monkeypatch.setattr(server, "_read_alarm_history", read)
    monkeypatch.setattr(server.json, "dumps", encode)
    assert asyncio.run(server.alarm_history(hours=2)).status_code == 200
    assert calls[0][1] - calls[0][0] == 7200
