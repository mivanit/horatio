"""Explicit retirement removes one held occurrence without suppressing recurrence."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest
from starlette.requests import Request

STAMP = 1_790_000_000.0
KEY = "fan/legacy"


def request(body: Any) -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": json.dumps(body).encode()}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/alarms/retire",
            "client": ("198.51.100.7", 1234),
        },
        receive,
    )


@pytest.fixture
def payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import server

    monkeypatch.setattr(server, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(
        server, "server_findings", lambda refresh=False: {"alarm_items": []}
    )
    monkeypatch.setattr(
        server, "_get_storage_health", lambda *args: {"status": "missing"}
    )
    server.PASSWORD_FILE.write_text(server._hash_password("correct"))
    memory: dict[str, Any] = {}
    events = server.observe_alarms(
        memory, "dashboard", [{"key": KEY, "text": "old fan stopped"}], [], STAMP
    )
    server.observe_alarms(memory, "dashboard", [], [], STAMP + 60)
    server._alarm_memory = memory
    server._save_alarm_memory(memory)
    server._append_events(events)
    return {
        "source": "dashboard",
        "key": KEY,
        "since": STAMP,
        "unchecked_since": STAMP + 60,
        "password": "correct",
    }


def test_retirement_persists_history_and_allows_recurrence(
    payload: dict[str, Any],
) -> None:
    import server

    response = asyncio.run(server.retire_alarm(request(payload)))
    assert response.status_code == 200
    assert KEY not in server._alarm_memory["dashboard"]["alarms"]
    loaded = server._load_alarm_memory()
    assert loaded == server._alarm_memory
    events = server._read_events()
    assert [event["event"] for event in events] == ["raised", "retired"]
    assert events[-1]["since"] == STAMP
    assert events[-1]["unchecked_since"] == STAMP + 60
    assert "password" not in events[-1]
    # No new evidence leaves retirement intact, including after a restart.
    assert server.observe_alarms(loaded, "dashboard", [], [], STAMP + 120) == []
    raised = server.observe_alarms(
        loaded,
        "dashboard",
        [{"key": KEY, "text": "fan stopped again"}],
        [KEY],
        STAMP + 180,
    )
    assert raised[0]["event"] == "raised"
    assert raised[0]["since"] == STAMP + 180
    assert not raised[0].get("initial")


def test_retirement_rejects_active_or_changed_occurrences(
    payload: dict[str, Any],
) -> None:
    import server

    old = json.loads(json.dumps(server._alarm_memory))
    for change in (
        {"since": STAMP - 1},
        {"unchecked_since": STAMP + 61},
        {"key": "unknown"},
    ):
        assert (
            asyncio.run(server.retire_alarm(request(payload | change))).status_code
            == 409
        )
        assert server._alarm_memory == old
    server.observe_alarms(
        server._alarm_memory,
        "dashboard",
        [{"key": KEY, "text": "fault returned"}],
        [KEY],
        STAMP + 120,
    )
    assert asyncio.run(server.retire_alarm(request(payload))).status_code == 409
    assert KEY in server._alarm_memory["dashboard"]["alarms"]


def test_retirement_requires_password_and_uses_throttle(
    payload: dict[str, Any],
) -> None:
    import server

    wrong = payload | {"password": "incorrect"}
    for _ in range(server.AUTH_MAX_FAILS):
        assert asyncio.run(server.retire_alarm(request(wrong))).status_code == 403
    assert asyncio.run(server.retire_alarm(request(payload))).status_code == 429
    server.PASSWORD_FILE.unlink()
    assert asyncio.run(server.retire_alarm(request(payload))).status_code == 503
    assert KEY in server._alarm_memory["dashboard"]["alarms"]
    assert [event["event"] for event in server._read_events()] == ["raised"]


def test_hidden_fault_resets_held_and_absence_bookkeeping() -> None:
    import server

    key = "journal/abcdef01"
    known, _ = server.track_alarms(
        None, [{"key": key, "text": "disk read failed"}], ["journal"], STAMP, 2
    )
    original = dict(known[key])
    known, _ = server.track_alarms(known, [], ["journal"], STAMP + 60, 2)
    known, _ = server.track_alarms(known, [], [], STAMP + 120, 2)
    assert known[key]["absent"] == 1
    assert known[key]["unchecked_since"] == STAMP + 120

    # The collector still sees this message, beyond its eight displayed rows.
    known, events = server.track_alarms(
        known, [], ["journal"], STAMP + 180, 2, hidden=[key]
    )
    assert known[key] == original
    assert events == []
    assert server.held_rows("state", None, {"state": {"alarms": known}}) == []

    # Confirmation of presence restarts the consecutive-absence requirement.
    known, events = server.track_alarms(known, [], ["journal"], STAMP + 240, 2)
    assert key in known
    assert events == []
    known, events = server.track_alarms(known, [], ["journal"], STAMP + 300, 2)
    assert key not in known
    assert [event["event"] for event in events] == ["cleared"]
    assert events[0]["since"] == STAMP


@pytest.mark.parametrize("source", ["state", "sensors", "dashboard"])
@pytest.mark.parametrize("field", ["alarm_items", "alarm_hidden"])
def test_new_active_observation_blocks_stale_retirement(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str, field: str
) -> None:
    import server

    server._alarm_memory[source] = server._alarm_memory["dashboard"]
    # The source has confirmed recurrence, but the tracker has not run yet.
    active = {
        field: (
            [KEY]
            if field == "alarm_hidden"
            else [{"key": KEY, "text": "fault returned"}]
        )
    }
    monkeypatch.setattr(server, "server_findings", lambda refresh=False: active)
    monkeypatch.setattr(server, "_get_storage_health", lambda *args: active)
    result = asyncio.run(server.retire_alarm(request(payload | {"source": source})))
    assert result.status_code == 409
    assert KEY in server._alarm_memory[source]["alarms"]
    assert [event["event"] for event in server._read_events()] == ["raised"]


@pytest.mark.parametrize(
    "change",
    [
        {"source": "services"},
        {"source": []},
        {"key": None},
        {"since": 10**1000},
        {"unchecked_since": False},
        {"password": 1},
    ],
)
def test_retirement_rejects_bad_requests(
    payload: dict[str, Any], change: dict[str, Any]
) -> None:
    import server

    assert (
        asyncio.run(server.retire_alarm(request(payload | change))).status_code == 400
    )
    assert KEY in server._alarm_memory["dashboard"]["alarms"]


@pytest.mark.parametrize("failed", ["_append_events", "_save_alarm_memory"])
def test_storage_failure_keeps_alarm_held(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failed: str
) -> None:
    import server

    def fail(*args: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(server, failed, fail)
    assert asyncio.run(server.retire_alarm(request(payload))).status_code == 500
    assert KEY in server._alarm_memory["dashboard"]["alarms"]
    assert KEY in server._load_alarm_memory()["dashboard"]["alarms"]


def test_duplicate_requests_retire_only_once(payload: dict[str, Any]) -> None:
    import server

    async def concurrent() -> list[int]:
        replies = await asyncio.gather(
            *(server.retire_alarm(request(payload)) for _ in range(2))
        )
        return sorted(reply.status_code for reply in replies)

    assert asyncio.run(concurrent()) == [200, 409]
    assert [event["event"] for event in server._read_events()] == ["raised", "retired"]


def test_cancellation_waits_for_persistence(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import server

    saving = threading.Event()
    release = threading.Event()
    save: Callable[..., None] = server._save_alarm_memory

    def delayed(memory: dict[str, Any]) -> None:
        saving.set()
        if not release.wait(5):
            raise RuntimeError("test did not release save")
        save(memory)

    monkeypatch.setattr(server, "_save_alarm_memory", delayed)

    async def interrupted() -> None:
        task = asyncio.create_task(server.retire_alarm(request(payload)))
        try:
            assert await asyncio.to_thread(saving.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert server._alarm_update_lock.locked()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted())
    assert not server._alarm_update_lock.locked()
    assert KEY not in server._alarm_memory["dashboard"]["alarms"]
    assert server._load_alarm_memory() == server._alarm_memory


def test_tracker_observation_and_retirement_cannot_overwrite_each_other(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import server

    reading = threading.Event()
    release = threading.Event()

    def new_fault() -> dict[str, Any]:
        reading.set()
        if not release.wait(5):
            raise RuntimeError("test did not release observation")
        return {
            "alarm_items": [{"key": KEY, "text": "fault returned"}],
            "checked": [KEY],
        }

    monkeypatch.setattr(
        server, "_get_storage_health", lambda *args: {"status": "missing"}
    )
    monkeypatch.setattr(server, "_server_findings_or_none", new_fault)

    async def overlapping() -> None:
        tracker = asyncio.create_task(server.track_alarms_once())
        try:
            assert await asyncio.to_thread(reading.wait, 5)
            retirement = asyncio.create_task(server.retire_alarm(request(payload)))
            await asyncio.sleep(0)
        finally:
            release.set()
        await tracker
        assert (await retirement).status_code == 409

    asyncio.run(overlapping())
    entry = server._alarm_memory["dashboard"]["alarms"][KEY]
    assert entry["text"] == "fault returned" and "unchecked_since" not in entry
    assert not any(event["event"] == "retired" for event in server._read_events())


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancelled_tracker_finishes_save_before_retirement(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch, cancellations: int
) -> None:
    import server

    saving = threading.Event()
    release = threading.Event()
    save: Callable[..., None] = server._save_alarm_memory

    def delayed_once(memory: dict[str, Any]) -> None:
        if not saving.is_set():
            saving.set()
            if not release.wait(5):
                raise RuntimeError("test did not release tracker save")
        save(memory)

    monkeypatch.setattr(server, "_save_alarm_memory", delayed_once)
    monkeypatch.setattr(
        server, "_server_findings_or_none", lambda: {"alarm_items": [], "checked": []}
    )
    # Keep the held occurrence recent and force persistence this pass.
    monkeypatch.setattr(server.time, "time", lambda: STAMP + 120)
    server._alarm_memory_saved = 0

    async def interrupted() -> None:
        tracker = asyncio.create_task(server.track_alarms_once())
        try:
            assert await asyncio.to_thread(saving.wait, 5)
            retirement = asyncio.create_task(server.retire_alarm(request(payload)))
            for attempt in range(cancellations):
                tracker.cancel(f"stop tracker {attempt}")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert server._alarm_update_lock.locked()
                assert not tracker.done() and not retirement.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError, match="stop tracker 0"):
            await tracker
        assert (await retirement).status_code == 200

    asyncio.run(interrupted())
    assert KEY not in server._alarm_memory["dashboard"]["alarms"]
    assert server._load_alarm_memory() == server._alarm_memory


def test_failed_save_does_not_replace_tracker_cancellation(
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import server

    saving = threading.Event()
    release = threading.Event()

    def failed_save(memory: dict[str, Any]) -> None:
        saving.set()
        if not release.wait(5):
            raise RuntimeError("test did not release tracker save")
        raise OSError("disk full")

    monkeypatch.setattr(server, "_save_alarm_memory", failed_save)
    monkeypatch.setattr(
        server, "_server_findings_or_none", lambda: {"alarm_items": [], "checked": []}
    )
    monkeypatch.setattr(server.time, "time", lambda: STAMP + 120)
    server._alarm_memory_saved = 0

    async def interrupted() -> None:
        tracker = asyncio.create_task(server.track_alarms_once())
        try:
            assert await asyncio.to_thread(saving.wait, 5)
            tracker.cancel("shutdown")
            await asyncio.sleep(0)
            assert server._alarm_update_lock.locked()
        finally:
            release.set()
        # OSError here would be caught by alarm_tracker, restarting its loop
        # while lifespan waits indefinitely for the cancelled tracker to exit.
        with pytest.raises(asyncio.CancelledError, match="shutdown"):
            await tracker

    asyncio.run(interrupted())
    assert not server._alarm_update_lock.locked()
    assert "disk full" in caplog.text


def test_failure_is_observed_when_cancellation_arrives_after_operation_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import server

    async def failed() -> None:
        await asyncio.sleep(0)
        raise OSError("completed save failed")

    async def interrupted() -> None:
        operation = asyncio.create_task(failed())
        waiter = asyncio.create_task(server._finish_alarm_update(operation))
        operation.add_done_callback(lambda done: waiter.cancel("shutdown"))
        with pytest.raises(asyncio.CancelledError, match="shutdown"):
            await waiter
        assert operation.done()

    asyncio.run(interrupted())
    assert "completed save failed" in caplog.text
