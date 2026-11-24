"""/api/alarms: one list from the two collector snapshots and the service
checks, the tail of the collectors' change log, and when an outage began."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def test_services_down_since_is_the_start_of_the_current_run_of_failures() -> None:
    import server

    rounds: list[tuple[float, dict[str, bool]]] = [
        (10, {"a": False, "b": True, "c": False}),
        (20, {"a": True, "b": True, "c": False}),
        (30, {"a": False, "b": True, "c": False}),
        (40, {"b": False}),  # a and c were not checked in this round
        (50, {"a": False, "b": False, "c": False}),
    ]
    assert server.services_down_since(rounds, ["a", "b", "c", "new"]) == {
        "a": 30,  # the earlier outage at 10 ended at 20
        "b": 40,
        "c": 10,  # down for as long as the rounds reach back
        "new": None,
    }
    rounds.append((60, {"a": True, "b": False, "c": False}))
    assert server.services_down_since(rounds, ["a", "b"]) == {"a": None, "b": 40}
    assert server.services_down_since([], ["a"]) == {"a": None}


def test_read_alarm_events_takes_the_tail_and_skips_what_is_not_an_event(
    tmp_path: Path,
) -> None:
    import server

    log: Path = tmp_path / "alarms.jsonl"
    assert server._read_alarm_events(str(log)) == [], "no log yet"
    lines: list[str] = [
        json.dumps({"timestamp": f"t{n}", "event": "raised", "key": f"k{n}"})
        for n in range(6)
    ]
    # A torn line, a line that is not an object, one without an event, and no
    # final newline (a collector is appending right now).
    log.write_text(
        "\n".join([*lines[:3], '{"timestamp": "t', "[1]", '{"key": "x"}', *lines[3:]])
    )
    events: list[dict[str, Any]] = server._read_alarm_events(str(log))
    assert [e["key"] for e in events] == [f"k{n}" for n in range(6)]
    assert [e["key"] for e in server._read_alarm_events(str(log), limit=2)] == [
        "k4",
        "k5",
    ]
    # Reading only the end of a long log drops the line the read began inside.
    size: int = len(lines[5]) + 10
    assert [e["key"] for e in server._read_alarm_events(str(log), max_bytes=size)] == [
        "k5"
    ]
    assert server._read_alarm_events(str(tmp_path)) == [], "unreadable: no events"


def snapshot(**fields: Any) -> dict[str, Any]:
    return {"status": "ok", "age_sec": 30, **fields}


def services(*targets: dict[str, Any], stale: bool = False) -> dict[str, Any]:
    return {"stale": stale, "targets": list(targets)}


def test_alarms_snapshot_merges_the_three_sources() -> None:
    import server

    now: float = time.time()
    state: dict[str, Any] = snapshot(
        alarms=["sde: reallocated=8"],
        alarm_items=[
            {
                "key": "smart/WFG16T45/reallocated",
                "text": "sde: reallocated=8",
                "value": 8,
                "since": 1795000000,
                "initial": True,
            }
        ],
        alarm_held=[
            {
                "key": "smart/S2/health",
                "text": "sdb: SMART health FAILED",
                "since": 5,
                "unchecked_since": 9,
            },
            {"key": "x", "text": "flap damping, not shown", "absent": 1},
        ],
    )
    sensors: dict[str, Any] = snapshot(
        alarms=["GPU 1: thermal slowdown active (sw)"],
        alarm_items=[
            {
                "key": "gpu/1/slowdown",
                "text": "GPU 1: thermal slowdown active (sw)",
                "since": 1795000100,
            }
        ],
    )
    checks: dict[str, Any] = services(
        {"name": "Immich", "ok": False, "error": "timeout", "down_since": 1795000200.5},
        {"name": "LuCI", "ok": True, "error": None, "down_since": None},
        {"name": "new", "ok": None, "error": None, "down_since": None},
    )
    events: list[dict[str, Any]] = [{"event": "raised", "key": "gpu/1/slowdown"}]
    out: dict[str, Any] = server.alarms_snapshot(now, state, sensors, checks, events)
    assert out["now"] == now and out["recent"] == events
    assert out["sources"] == {
        "state": {"status": "ok", "age_sec": 30},
        "sensors": {"status": "ok", "age_sec": 30},
        "services": {"status": "ok", "targets": 3},
    }
    assert out["active"] == [
        {
            "source": "state",
            "key": "smart/WFG16T45/reallocated",
            "text": "sde: reallocated=8",
            "value": 8,
            "since": 1795000000,
            "initial": True,
        },
        {
            "source": "sensors",
            "key": "gpu/1/slowdown",
            "text": "GPU 1: thermal slowdown active (sw)",
            "value": None,
            "since": 1795000100,
            "initial": None,
        },
        {
            "source": "services",
            "key": "service/Immich",
            "text": "Immich: down (timeout)",
            "value": None,
            "since": 1795000200.5,
            "initial": None,
        },
    ]
    assert out["held"] == [
        {
            "source": "state",
            "key": "smart/S2/health",
            "text": "sdb: SMART health FAILED",
            "value": None,
            "since": 5,
            "initial": None,
            "unchecked_since": 9,
        }
    ]


def test_alarms_snapshot_never_reads_an_unknown_source_as_clear() -> None:
    import server

    state: dict[str, Any] = snapshot(alarms=["md0: DEGRADED (1 missing)"])
    hot: dict[str, Any] = snapshot(alarms=["GPU 0: retired pages pending"])
    down: dict[str, Any] = services(
        {"name": "Immich", "ok": False, "error": "timeout", "down_since": 1.0}
    )

    def texts(
        state: dict[str, Any], sensors: dict[str, Any], checks: dict[str, Any]
    ) -> list[str]:
        merged: dict[str, Any] = server.alarms_snapshot(0, state, sensors, checks, [])
        return [a["text"] for a in merged["active"]]

    # A collector from before alarm_items: the text is all there is.
    old: dict[str, Any] = server.alarms_snapshot(0, state, hot, down, [])
    assert [(a["key"], a["since"]) for a in old["active"]] == [(None, None)] * 2 + [
        ("service/Immich", 1.0)
    ]
    # The card's rule: a stale 15-min snapshot still shows what it last saw (and
    # says it is stale); a stale 60 s one and stale checks are unknown.
    assert texts({**state, "status": "stale"}, hot, down) == [
        "md0: DEGRADED (1 missing)",
        "GPU 0: retired pages pending",
        "Immich: down (timeout)",
    ]
    stale: dict[str, Any] = server.alarms_snapshot(
        0, state, {**hot, "status": "stale"}, {**down, "stale": True}, []
    )
    assert [a["text"] for a in stale["active"]] == ["md0: DEGRADED (1 missing)"]
    assert stale["sources"]["sensors"]["status"] == "stale"
    assert stale["sources"]["services"]["status"] == "stale"
    # No collector installed at all.
    missing: dict[str, Any] = {"status": "missing", "age_sec": None}
    assert texts(missing, missing, services()) == []
    # Junk in a snapshot is skipped, not served.
    junk: dict[str, Any] = snapshot(
        alarms=["a"], alarm_items=[{"key": "k"}, "x", {"key": "k2", "text": "b"}]
    )
    assert texts(junk, missing, services()) == ["b"]
