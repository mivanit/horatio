"""The alarm history: when each alarm started, changed and cleared, whoever raised
it; the event log; /api/alarms as one list; and when a service outage began."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest


def item(key: str, text: str = "", value: Any = None) -> dict[str, Any]:
    return {
        "key": key,
        "text": text or key,
        **({"value": value} if value is not None else {}),
    }


def kinds(events: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(e["event"], e.get("key")) for e in events]


# ---- one source's alarms against what was known of them -------------------------


def test_first_sight_is_initial_then_since_is_carried_and_changes_are_events() -> None:
    import server

    known: dict[str, dict[str, Any]]
    events: list[dict[str, Any]]
    known, events = server.track_alarms(
        None, [item("disk", "sde: reallocated=8", 8)], [], 100, 1
    )
    assert known == {
        "disk": {
            "key": "disk",
            "text": "sde: reallocated=8",
            "value": 8,
            "since": 100,
            "initial": True,
        }
    }
    assert events == [{"event": "raised", **known["disk"]}]

    # Still there with another text and the same value: nothing happened.
    same: dict[str, dict[str, Any]]
    same, events = server.track_alarms(
        known, [item("disk", "sdf: reallocated=8", 8)], [], 200, 1
    )
    assert events == []
    assert (same["disk"]["since"], same["disk"]["text"], same["disk"]["initial"]) == (
        100,
        "sdf: reallocated=8",
        True,
    )
    assert known["disk"]["text"] == "sde: reallocated=8", "the input is not changed"

    # The counter moved, and a second alarm appeared: that one has a real start.
    moved: dict[str, dict[str, Any]]
    moved, events = server.track_alarms(
        same,
        [item("disk", "sdf: reallocated=9", 9), item("md", "md0: DEGRADED")],
        [],
        300,
        1,
    )
    assert events == [
        {
            "event": "changed",
            "key": "disk",
            "text": "sdf: reallocated=9",
            "value": 9,
            "since": 100,
            "initial": True,
            "was": 8,
        },
        {"event": "raised", "key": "md", "text": "md0: DEGRADED", "since": 300},
    ]
    # Junk in a snapshot is skipped, not tracked.
    assert server.track_alarms({}, ["x", {"text": "no key"}, item("ok")], [], 1, 1)[
        0
    ].keys() == {"ok"}


def test_gone_is_cleared_only_when_checked() -> None:
    import server

    known: dict[str, dict[str, Any]] = server.track_alarms(
        {}, [item("smart/S1/health"), item("unit/x.service")], [], 100, 1
    )[0]
    # The drive is asleep (not in `checked`); the unit list was read.
    after: dict[str, dict[str, Any]]
    events: list[dict[str, Any]]
    after, events = server.track_alarms(known, [], ["unit"], 1000, 1)
    assert events == [
        {
            "event": "cleared",
            "key": "unit/x.service",
            "text": "unit/x.service",
            "since": 100,
            "active_for_s": 900,
        }
    ]
    assert after == {
        "smart/S1/health": {
            "key": "smart/S1/health",
            "text": "smart/S1/health",
            "since": 100,
            "unchecked_since": 1000,
        }
    }
    # Still asleep: the first unchecked time stays. Awake and failing: the same alarm.
    after, events = server.track_alarms(after, [], ["unit"], 2000, 1)
    assert (after["smart/S1/health"]["unchecked_since"], events) == (1000, [])
    back: dict[str, dict[str, Any]]
    back, events = server.track_alarms(
        after, [item("smart/S1/health")], ["smart/S1"], 3000, 1
    )
    assert (back["smart/S1/health"], events) == (
        {"key": "smart/S1/health", "text": "smart/S1/health", "since": 100},
        [],
    )
    # A prefix covers a key only at a "/" boundary.
    assert server.track_alarms(known, [], ["smart/S", "uni"], 1000, 1)[1] == []
    # Asleep for more than 30 days: dropped, and the log says so.
    late: float = 1000 + server.ALARM_HELD_EXPIRE_SEC + 1
    after, events = server.track_alarms(after, [], [], late, 1)
    assert after == {} and kinds(events) == [("expired", "smart/S1/health")]
    assert events[0]["unchecked_since"] == 1000
    # `checked` None: a source that judges everything every time.
    assert kinds(server.track_alarms(known, [], None, 500, 1)[1]) == [
        ("cleared", "smart/S1/health"),
        ("cleared", "unit/x.service"),
    ]


def test_a_flapping_alarm_must_be_absent_three_times_to_clear() -> None:
    import server

    known: dict[str, dict[str, Any]] = server.track_alarms(
        {}, [item("gpu/0/temp/core")], [], 100, 3
    )[0]
    events: list[dict[str, Any]]
    now: int
    for now, absent in ((160, 1), (220, 2)):
        known, events = server.track_alarms(known, [], ["gpu"], now, 3)
        assert (known["gpu/0/temp/core"]["absent"], events) == (absent, [])
    # Back before the third: the same alarm, the same start, no line written.
    known, events = server.track_alarms(
        known, [item("gpu/0/temp/core")], ["gpu"], 280, 3
    )
    assert (known["gpu/0/temp/core"], events) == (
        {"key": "gpu/0/temp/core", "text": "gpu/0/temp/core", "since": 100},
        [],
    )
    for now in (340, 400):
        known, events = server.track_alarms(known, [], ["gpu"], now, 3)
    known, events = server.track_alarms(known, [], ["gpu"], 460, 3)
    assert known == {} and kinds(events) == [("cleared", "gpu/0/temp/core")]


def test_a_clear_after_a_reboot_says_so_however_late_it_comes() -> None:
    import server

    # The 60 s source clears on the third absence, long after the first snapshot
    # of the new boot: the alarm remembers the boot it was last seen on.
    known: dict[str, dict[str, Any]] = server.track_alarms(
        {}, [item("gpu/0/ecc")], [], 100, 3, "boot-a"
    )[0]
    assert known["gpu/0/ecc"]["boot"] == "boot-a"
    events: list[dict[str, Any]] = []
    now: int
    for now in (160, 220, 280):
        known, events = server.track_alarms(known, [], ["gpu"], now, 3, "boot-b")
    assert [(e["event"], e.get("after_boot")) for e in events] == [("cleared", True)]
    # The same boot throughout, or a boot nobody named: not said.
    for boots in (("boot-a", "boot-a"), ("boot-a", None), (None, "boot-b")):
        known = server.track_alarms({}, [item("x")], [], 100, 1, boots[0])[0]
        assert (
            "after_boot"
            not in server.track_alarms(known, [], ["x"], 200, 1, boots[1])[1][0]
        )
    # Still there on the new boot: it belongs to that boot from now on.
    known = server.track_alarms({}, [item("x")], [], 100, 1, "boot-a")[0]
    known = server.track_alarms(known, [item("x")], [], 200, 1, "boot-b")[0]
    assert (
        "after_boot"
        not in server.track_alarms(known, [], ["x"], 300, 1, "boot-b")[1][0]
    )


def test_hidden_keys_stay_and_everything_else_under_a_checked_prefix_goes() -> None:
    import server

    # A journal storm: ten messages known, the collector prints eight and says
    # the other two still exist. Next run, five of the ten are no longer there.
    known: dict[str, dict[str, Any]] = server.track_alarms(
        {}, [item(f"journal/{n}") for n in range(10)], ["journal"], 100, 1
    )[0]
    after: dict[str, dict[str, Any]]
    events: list[dict[str, Any]]
    after, events = server.track_alarms(
        known,
        [item(f"journal/{n}") for n in range(3)],
        ["journal"],
        1000,
        1,
        hidden=["journal/3", "journal/4"],
    )
    assert sorted(after) == [f"journal/{n}" for n in range(5)]
    assert after["journal/4"] == known["journal/4"], "a hidden alarm is left as it was"
    assert sorted(e["key"] for e in events) == [f"journal/{n}" for n in range(5, 10)]
    assert {e["event"] for e in events} == {"cleared"}


def test_odd_input_is_dropped_not_tracked_wrongly() -> None:
    import server

    known: dict[str, dict[str, Any]]
    events: list[dict[str, Any]]
    # The same key twice in one snapshot: the first one counts, and the next
    # snapshot does not raise it again.
    twice: list[dict[str, Any]] = [item("k", "first", 1), item("k", "second", 2)]
    known, events = server.track_alarms({}, twice, [], 100, 1)
    assert (known["k"]["text"], kinds(events)) == ("first", [("raised", "k")])
    assert server.track_alarms(known, twice, [], 200, 1)[1] == []
    # A value that is not a count or a word is not kept (NaN would also make the
    # API's JSON unreadable, and never equal itself).
    nan: dict[str, Any] = {"key": "n", "text": "t", "value": float("nan")}
    known, events = server.track_alarms(
        {}, [nan, {"key": "l", "text": "t", "value": [1]}], [], 100, 1
    )
    assert "value" not in known["n"] and "value" not in known["l"]
    assert server.track_alarms(known, [nan], ["l"], 200, 1)[1][0]["event"] == "cleared"
    assert server.alarm_rows("state", [nan, {"key": ["x"], "text": "T"}], {})[1] == {
        "source": "state",
        "key": None,
        "text": "T",
        "value": None,
        "since": None,
        "initial": None,
    }
    assert server.alarm_rows("state", None, {}, texts="not a list") == []
    # The clock stepped back between raise and clear: no negative duration.
    known = server.track_alarms({}, [item("c")], [], 5000, 1)[0]
    assert server.track_alarms(known, [], ["c"], 4000, 1)[1][0]["active_for_s"] == 0


def test_a_key_checked_only_every_other_time_still_clears() -> None:
    import server

    known: dict[str, dict[str, Any]] = server.track_alarms({}, [item("x")], [], 0, 3)[0]
    events: list[dict[str, Any]] = []
    n: int
    for n in range(1, 6):  # unchecked, checked, unchecked, checked, unchecked, ...
        known, events = server.track_alarms(
            known, [], ["x"] if n % 2 == 0 else [], n * 60, 3
        )
        assert events == []
    assert known["x"]["absent"] == 2
    known, events = server.track_alarms(known, [], ["x"], 360, 3)
    assert kinds(events) == [("cleared", "x")] and known == {}


# ---- snapshots of a source over time ---------------------------------------------


def test_observe_stamps_events_and_logs_gaps_and_boots() -> None:
    import server

    memory: dict[str, Any] = {}
    boot: dict[str, Any] = {
        "id": "b2",
        "kernel": "6.8.0-149",
        "previous": {"clean": False},
    }
    first: list[dict[str, Any]] = server.observe_alarms(
        memory,
        "state",
        [item("ecc/uncorrectable", "ECC: 1", 1)],
        ["ecc"],
        1000,
        "b1",
        {"id": "b1"},
    )
    assert first == [
        {
            "timestamp": "1970-01-01T00:16:40+00:00",
            "source": "state",
            "event": "raised",
            "key": "ecc/uncorrectable",
            "text": "ECC: 1",
            "value": 1,
            "since": 1000,
            "initial": True,
        }
    ]
    assert memory["state"]["ts"] == 1000 and memory["state"]["boot_id"] == "b1"
    # 15 minutes later, nothing new: no lines.
    assert (
        server.observe_alarms(
            memory,
            "state",
            [item("ecc/uncorrectable", "ECC: 1", 1)],
            ["ecc"],
            1900,
            "b1",
        )
        == []
    )
    # Three hours later, on a new boot whose counters start from zero.
    later: list[dict[str, Any]] = server.observe_alarms(
        memory, "state", [], ["ecc"], 1900 + 3 * 3600, "b2", boot
    )
    assert [(e["event"], e["source"]) for e in later] == [
        ("gap", "state"),
        ("boot", "state"),
        ("cleared", "state"),
    ]
    assert (later[0]["from"], later[0]["to"]) == (
        "1970-01-01T00:31:40+00:00",
        "1970-01-01T03:31:40+00:00",
    )
    assert (later[1]["id"], later[1]["previous"]) == ("b2", {"clean": False})
    assert later[2]["after_boot"] is True
    assert "boot" not in later[2], "which boot an alarm was seen on is not for the log"
    # The 60 s source calls three missed minutes a gap; two are not.
    server.observe_alarms(memory, "sensors", [], [], 1000)
    assert server.observe_alarms(memory, "sensors", [], [], 1120) == []
    assert kinds(server.observe_alarms(memory, "sensors", [], [], 1400)) == [
        ("gap", None)
    ]


def test_memory_file_round_trips_and_a_bad_one_starts_over(tmp_path: Path) -> None:
    import server

    path: Path = tmp_path / "logs" / "alarms-state.json"
    assert server._load_alarm_memory(path) == {}, "no file yet"
    memory: dict[str, Any] = {}
    server.observe_alarms(memory, "state", [item("a")], [], 1000, "b1")
    server._save_alarm_memory(memory, path)
    assert server._load_alarm_memory(path) == memory
    assert [p.name for p in path.parent.iterdir()] == ["alarms-state.json"], (
        "no temp file left"
    )
    path.write_text("{not json")
    assert server._load_alarm_memory(path) == {}
    # One entry that makes no sense costs only itself, not every later pass.
    good: dict[str, Any] = memory["state"]["alarms"]["a"]
    odd: dict[str, Any] = {
        "a": good,
        "b": {**good, "key": "b", "absent": "two"},
        "c": {**good, "key": "c", "unchecked_since": "yesterday"},
        "d": {**good, "key": "d", "since": 1e18},
        "e": {**good, "key": "not e"},
        "f": "x",
    }
    path.write_text(
        json.dumps({"state": {**memory["state"], "boot_id": 7, "alarms": odd}})
    )
    assert server._load_alarm_memory(path) == {
        "state": {"ts": 1000, "boot_id": None, "alarms": {"a": good}}
    }
    # Only what the tracker could have written is taken back.
    path.write_text(
        json.dumps(
            {
                "state": memory["state"],
                "elsewhere": memory["state"],
                "sensors": {"ts": "x", "alarms": {}},
            }
        )
    )
    assert server._load_alarm_memory(path).keys() == {"state"}


def snapshot(
    ts: float, items: list[dict[str, Any]], checked: list[str], **more: Any
) -> dict[str, Any]:
    return {
        "status": "ok",
        "age_sec": 1,
        "schema": 1,
        "ts": ts,
        "alarms": [i["text"] for i in items],
        "alarm_items": items,
        "checked": checked,
        **more,
    }


def test_the_tracker_follows_all_three_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    disk: dict[str, Any] = item("smart/S1/reallocated", "sde: reallocated=8", 8)
    world: dict[str, Any] = {
        "state": snapshot(1000, [disk], ["smart/S1"], boot_id="b1"),
        # A collector from before alarm keys: nothing to go by, so not tracked.
        "sensors": {"status": "ok", "age_sec": 1, "ts": 1000, "alarms": ["GPU 0: hot"]},
        "fans": [],
    }
    monkeypatch.setattr(
        server,
        "_get_storage_health",
        lambda path=server.STATE_PATH, stale=0: world[
            "sensors" if path == server.SENSORS_PATH else "state"
        ],
    )
    monkeypatch.setattr(
        server,
        "get_hw_sensors",
        lambda: {
            "alarm_items": world["fans"],
            "checked": world.get("readable", ["fan/cpu"]),
        },
    )

    def log() -> list[tuple[str, str, str | None]]:
        return [(e["source"], e["event"], e.get("key")) for e in server._read_events()]

    def run() -> None:
        asyncio.run(server.track_alarms_once())

    run()
    assert log() == [("state", "raised", "smart/S1/reallocated")]
    assert server._alarm_memory.keys() == {"state", "dashboard"}
    run()  # the same snapshots again: nothing to say
    assert len(log()) == 1
    # The collector ran again and the count grew; a fan fell below its minimum.
    world["state"] = snapshot(
        1900,
        [{**disk, "text": "sde: reallocated=9", "value": 9}],
        ["smart/S1"],
        boot_id="b1",
    )
    world["fans"] = [item("fan/cpu", "fan cpu: 0 RPM, below its 300 RPM minimum")]
    run()
    assert log()[1:] == [
        ("state", "changed", "smart/S1/reallocated"),
        ("dashboard", "raised", "fan/cpu"),
    ]
    fan: dict[str, Any] = server._alarm_memory["dashboard"]["alarms"]["fan/cpu"]
    assert "initial" not in fan and abs(fan["since"] - time.time()) < 60
    # What the tracker knows survives a restart of the server.
    assert server._load_alarm_memory() == server._alarm_memory

    # The log cannot be written: the memory does not move on, so nothing is lost.
    world["state"] = snapshot(2800, [], ["smart/S1"], boot_id="b1")
    before: dict[str, Any] = server._alarm_memory
    real_append: Any = server._append_events

    def full_disk(events: list[dict[str, Any]]) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(server, "_append_events", full_disk)
    with pytest.raises(OSError):
        run()
    assert server._alarm_memory is before and len(log()) == 3
    monkeypatch.setattr(server, "_append_events", real_append)
    run()
    assert log()[3] == ("state", "cleared", "smart/S1/reallocated")
    # The log was written but the memory could not be saved: the lines are not
    # written a second time while the server keeps running.
    world["state"] = snapshot(3700, [disk], ["smart/S1"], boot_id="b1")
    real_save: Any = server._save_alarm_memory

    def cannot_save(memory: dict[str, Any]) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(server, "_save_alarm_memory", cannot_save)
    with pytest.raises(OSError):
        run()
    monkeypatch.setattr(server, "_save_alarm_memory", real_save)
    run()
    assert log()[4:] == [("state", "raised", "smart/S1/reallocated")]
    world["state"] = snapshot(4600, [], ["smart/S1"], boot_id="b1")
    run()
    assert log()[5] == ("state", "cleared", "smart/S1/reallocated")

    # The fan stopped answering: its alarm is not listed, and not known to be gone.
    world["fans"], world["readable"] = [], []
    lines: int = len(log())
    for _ in range(4):
        run()
    assert len(log()) == lines
    assert "unchecked_since" in server._alarm_memory["dashboard"]["alarms"]["fan/cpu"]
    # It answers again and is fine, three passes in a row.
    world["readable"] = ["fan/cpu"]
    run()
    run()
    assert len(log()) == lines
    run()
    assert log()[lines] == ("dashboard", "cleared", "fan/cpu")

    # One source that makes no sense costs only itself: the others are followed.
    world["state"] = snapshot(5500, [disk], ["smart/S1"], boot_id="b1")
    world["sensors"] = snapshot(5500, [item("gpu/0/slowdown")], ["gpu"])
    monkeypatch.setattr(server, "observe_alarms", flaky(server.observe_alarms))
    run()
    assert log()[lines + 1 :] == [("sensors", "raised", "gpu/0/slowdown")]


def flaky(real: Any) -> Any:
    """observe_alarms that fails for the 15-min source only."""

    def observe(memory: dict[str, Any], source: str, *args: Any, **kwargs: Any) -> Any:
        if source == "state":
            raise ValueError("a snapshot nobody expected")
        return real(memory, source, *args, **kwargs)

    return observe


def test_a_gap_is_the_trackers_own_when_the_tracker_was_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    now: float = time.time()
    world: dict[str, Any] = {
        "state": snapshot(now - 4000, [], []),
        "sensors": snapshot(now - 4000, [], []),
    }
    monkeypatch.setattr(
        server,
        "_get_storage_health",
        lambda path=server.STATE_PATH, stale=0: world[
            "sensors" if path == server.SENSORS_PATH else "state"
        ],
    )
    monkeypatch.setattr(
        server, "get_hw_sensors", lambda: {"alarm_items": [], "checked": []}
    )

    def gaps() -> list[str]:
        return [e["source"] for e in server._read_events() if e["event"] == "gap"]

    asyncio.run(server.track_alarms_once())
    # The server was down for an hour; both collectors kept running meanwhile.
    server._alarm_memory["dashboard"]["ts"] = now - 3600
    world["state"] = snapshot(now - 10, [], [])
    world["sensors"] = snapshot(now - 10, [], [])
    asyncio.run(server.track_alarms_once())
    assert gaps() == ["dashboard"], "not blamed on the collectors"
    # The server runs; the 60 s collector stops for ten minutes and comes back.
    world["sensors"] = snapshot(now + 600, [], [])
    asyncio.run(server.track_alarms_once())
    assert gaps() == ["dashboard", "sensors"]


# ---- what the API serves -----------------------------------------------------------


def test_read_events_takes_the_tail_and_skips_what_is_not_an_event(
    tmp_path: Path,
) -> None:
    import server

    log: Path = tmp_path / "events.jsonl"
    assert server._read_events(log) == [], "no log yet"
    lines: list[str] = [
        json.dumps({"timestamp": f"t{n}", "event": "raised", "key": f"k{n}"})
        for n in range(6)
    ]
    # A torn line, a line that is not an object, one without an event, and no
    # final newline (a line is being appended right now).
    log.write_text(
        "\n".join([*lines[:3], '{"timestamp": "t', "[1]", '{"key": "x"}', *lines[3:]])
    )
    assert [e["key"] for e in server._read_events(log)] == [f"k{n}" for n in range(6)]
    assert [e["key"] for e in server._read_events(log, limit=2)] == ["k4", "k5"]
    # Reading only the end of a long log drops the line the read began inside.
    assert [
        e["key"] for e in server._read_events(log, max_bytes=len(lines[5]) + 10)
    ] == ["k5"]
    assert server._read_events(tmp_path) == [], "unreadable: no events"


def services(*targets: dict[str, Any], stale: bool = False) -> dict[str, Any]:
    return {"stale": stale, "targets": list(targets)}


def test_snapshots_are_served_with_since_and_what_could_not_be_rechecked() -> None:
    import server

    memory: dict[str, Any] = {}
    server.observe_alarms(
        memory, "state", [item("a", "A", 1), item("b", "B")], [], 1000
    )
    live: dict[str, Any] = snapshot(1900, [item("a", "A now", 2), item("new", "N")], [])
    server.observe_alarms(memory, "state", live["alarm_items"][:1], [], 1900)
    served: dict[str, Any] = server.with_alarm_history("state", live, memory)
    assert served["alarm_items"] == [
        {"key": "a", "text": "A now", "value": 2, "since": 1000, "initial": True},
        # Not seen by the tracker yet (it runs every 30 s): no start to give.
        {"key": "new", "text": "N"},
    ]
    assert served["alarm_held"] == [
        {
            "key": "b",
            "text": "B",
            "since": 1000,
            "initial": True,
            "unchecked_since": 1900,
        }
    ]
    assert served["alarms"] == live["alarms"] and "alarm_held" not in live
    # A collector without alarm keys, or one newer than this server understands,
    # is served as it is: what the tracker remembers of it is old.
    old: dict[str, Any] = {"status": "ok", "ts": 1, "alarms": ["x"]}
    assert server.with_alarm_history("state", old, memory) is old
    newer: dict[str, Any] = {**live, "schema": 2}
    assert server.with_alarm_history("state", newer, memory) is newer
    assert server.held_rows("state", {"status": "missing"}, memory) == []


def test_alarms_snapshot_merges_the_four_sources() -> None:
    import server

    memory: dict[str, Any] = {}
    disk: dict[str, Any] = item("smart/WFG16T45/reallocated", "sde: reallocated=8", 8)
    gpu: dict[str, Any] = item("gpu/1/slowdown", "GPU 1: thermal slowdown active (sw)")
    fan: dict[str, Any] = item("fan/cpu", "fan cpu: 0 RPM, below its 300 RPM minimum")
    server.observe_alarms(
        memory,
        "state",
        [disk, item("smart/S2/health", "sdb: SMART health FAILED")],
        [],
        1000,
    )
    server.observe_alarms(memory, "state", [disk], [], 1900)
    server.observe_alarms(memory, "sensors", [], [], 1000)
    server.observe_alarms(memory, "sensors", [gpu], [], 1060)
    server.observe_alarms(memory, "dashboard", [], None, 1000)
    server.observe_alarms(memory, "dashboard", [fan], None, 1030)
    checks: dict[str, Any] = services(
        {"name": "Immich", "ok": False, "error": "timeout", "down_since": 1200.5},
        {"name": "LuCI", "ok": True, "error": None, "down_since": None},
        {"name": "new", "ok": None, "error": None, "down_since": None},
    )
    events: list[dict[str, Any]] = [{"event": "raised", "key": "gpu/1/slowdown"}]
    out: dict[str, Any] = server.alarms_snapshot(
        2000,
        snapshot(1900, [disk], []),
        snapshot(1960, [gpu], []),
        [fan],
        checks,
        memory,
        events,
    )
    assert out["now"] == 2000 and out["recent"] == events
    assert out["sources"] == {
        "state": {"status": "ok", "age_sec": 1},
        "sensors": {"status": "ok", "age_sec": 1},
        "dashboard": {"status": "ok"},
        "services": {"status": "ok", "targets": 3},
    }
    assert [
        (a["source"], a["key"], a["since"], a["initial"]) for a in out["active"]
    ] == [
        ("state", "smart/WFG16T45/reallocated", 1000, True),
        ("sensors", "gpu/1/slowdown", 1060, None),
        ("dashboard", "fan/cpu", 1030, None),
        ("services", "service/Immich", 1200.5, None),
    ]
    assert out["active"][0] == {
        "source": "state",
        "key": "smart/WFG16T45/reallocated",
        "text": "sde: reallocated=8",
        "value": 8,
        "since": 1000,
        "initial": True,
    }
    assert out["active"][3]["text"] == "Immich: down (timeout)"
    assert out["held"] == [
        {
            "source": "state",
            "key": "smart/S2/health",
            "text": "sdb: SMART health FAILED",
            "value": None,
            "since": 1000,
            "initial": True,
            "unchecked_since": 1900,
        }
    ]


def test_alarms_snapshot_never_reads_an_unknown_source_as_clear() -> None:
    import server

    state: dict[str, Any] = {
        "status": "ok",
        "age_sec": 1,
        "alarms": ["md0: DEGRADED (1 missing)"],
    }
    hot: dict[str, Any] = {
        "status": "ok",
        "age_sec": 1,
        "alarms": ["GPU 0: retired pages pending"],
    }
    down: dict[str, Any] = services(
        {"name": "Immich", "ok": False, "error": "timeout", "down_since": 1.0}
    )

    def merged(
        a: dict[str, Any], b: dict[str, Any], c: dict[str, Any]
    ) -> dict[str, Any]:
        return server.alarms_snapshot(0, a, b, [], c, {}, [])

    def texts(a: dict[str, Any], b: dict[str, Any], c: dict[str, Any]) -> list[str]:
        return [row["text"] for row in merged(a, b, c)["active"]]

    # Collectors from before alarm keys: the text is all there is.
    assert [(a["key"], a["since"]) for a in merged(state, hot, down)["active"]] == [
        (None, None),
        (None, None),
        ("service/Immich", 1.0),
    ]
    # The card's rule: a stale 15-min snapshot still shows what it last saw (and
    # says it is stale); a stale 60 s one and stale checks are unknown.
    assert texts({**state, "status": "stale"}, hot, down) == [
        "md0: DEGRADED (1 missing)",
        "GPU 0: retired pages pending",
        "Immich: down (timeout)",
    ]
    stale: dict[str, Any] = merged(
        state, {**hot, "status": "stale"}, {**down, "stale": True}
    )
    assert [a["text"] for a in stale["active"]] == ["md0: DEGRADED (1 missing)"]
    assert stale["sources"]["sensors"]["status"] == "stale"
    assert stale["sources"]["services"]["status"] == "stale"
    # No collector installed at all.
    missing: dict[str, Any] = {"status": "missing", "age_sec": None}
    assert texts(missing, missing, services()) == []


# ---- when a service outage began ------------------------------------------------------


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
