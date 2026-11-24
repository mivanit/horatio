"""Review only known event evidence; new evidence must demand attention again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_alarm_retirement import request

STAMP = 1_790_000_000.0
KEY = "journal/test"


@pytest.fixture
def evidence_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any]]:
    import server

    monkeypatch.setattr(server, "PBKDF2_ITERATIONS", 1000)
    server.PASSWORD_FILE.write_text(server._hash_password("correct"))
    item = {"key": KEY, "text": "journal: disk read failed", "evidence": ["a", "b"]}
    snapshot = {
        "status": "ok",
        "schema": 1,
        "ts": STAMP,
        "alarm_items": [item],
        "alarms": [item["text"]],
        "checked": ["journal"],
    }
    memory: dict[str, Any] = {}
    server._append_events(
        server.observe_alarms(memory, "state", [item], ["journal"], STAMP)
    )
    server._alarm_memory = memory
    server._save_alarm_memory(memory)
    monkeypatch.setattr(server, "_get_storage_health", lambda *args: snapshot)
    body = {
        "source": "state",
        "key": KEY,
        "since": STAMP,
        "evidence": ["a", "b"],
        "password": "correct",
        "note": "Checked cabling",
    }
    return body, snapshot


def test_review_persists_note_and_removes_only_reviewed_evidence(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, snapshot = evidence_alarm
    assert asyncio.run(server.review_alarm(request(body))).status_code == 200
    assert server._load_alarm_memory() == server._alarm_memory
    result = server.with_alarm_history("state", snapshot, server._alarm_memory)
    assert result["alarms"] == [] and result["alarm_items"] == []
    assert result["alarm_reviewed"][0]["reviewed"]["note"] == body["note"]
    last = server._read_events()[-1]
    assert last["event"] == "reviewed" and last["note"] == body["note"]
    assert "password" not in last
    merged = server.alarms_snapshot(
        STAMP,
        snapshot,
        {"status": "missing"},
        [],
        {"stale": False, "targets": []},
        server._alarm_memory,
        [],
    )
    assert merged["active"] == []
    assert merged["reviewed"][0]["key"] == KEY


def test_aging_out_old_evidence_keeps_review_but_new_evidence_reopens(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, snapshot = evidence_alarm
    asyncio.run(server.review_alarm(request(body)))
    known = server._load_alarm_memory()["state"]["alarms"]
    item = snapshot["alarm_items"][0] | {"evidence": ["b"]}
    known, events = server.track_alarms(known, [item], ["journal"], STAMP + 60, 1)
    assert events == [] and "reviewed" in known[KEY]
    item["evidence"] = ["b", "c"]
    # Fresh API data cannot stay hidden while waiting for the next tracker pass.
    assert "reviewed" not in server.alarm_rows("state", [item], server._alarm_memory)[0]
    known, events = server.track_alarms(known, [item], ["journal"], STAMP + 120, 1)
    assert "reviewed" not in known[KEY]
    assert [(row["event"], row["reason"]) for row in events] == [
        ("reopened", "new_evidence")
    ]
    assert known[KEY]["since"] == STAMP


def test_unavailable_evidence_does_not_suppress_alarm(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, snapshot = evidence_alarm
    asyncio.run(server.review_alarm(request(body)))
    item = {"key": KEY, "text": "journal read partial"}
    assert "reviewed" not in server.alarm_rows("state", [item], server._alarm_memory)[0]
    known, events = server.track_alarms(
        server._alarm_memory["state"]["alarms"], [item], [], STAMP + 60, 1
    )
    assert "reviewed" not in known[KEY]
    assert events[0]["reason"] == "evidence_unavailable"


def test_review_rejects_new_or_stale_evidence(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, snapshot = evidence_alarm
    snapshot["alarm_items"][0]["evidence"] = ["a", "b", "c"]
    assert asyncio.run(server.review_alarm(request(body))).status_code == 409
    snapshot["alarm_items"][0]["evidence"] = body["evidence"]
    snapshot["status"] = "stale"
    assert asyncio.run(server.review_alarm(request(body))).status_code == 409
    assert "reviewed" not in server._alarm_memory["state"]["alarms"][KEY]


def test_web_review_requires_password_and_rejects_live_conditions(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, _ = evidence_alarm
    assert (
        asyncio.run(
            server.review_alarm(request(body | {"password": "wrong"}))
        ).status_code
        == 403
    )
    assert (
        asyncio.run(server.review_alarm(request(body | {"key": "fan/x"}))).status_code
        == 400
    )
    assert (
        asyncio.run(
            server.review_alarm(request(body | {"note": "x" * 2001}))
        ).status_code
        == 400
    )
    server.PASSWORD_FILE.unlink()
    assert asyncio.run(server.review_alarm(request(body))).status_code == 503


def test_file_review_uses_same_rules_and_writes_a_receipt(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    import server

    body, _ = evidence_alarm
    directory = tmp_path / "actions"
    directory.mkdir()
    body.pop("password")
    server.PASSWORD_FILE.unlink()  # local file ownership is the authorization
    command = directory / "operator-1.json"
    command.write_text(json.dumps(body | {"action": "review"}))
    asyncio.run(server.process_alarm_actions(directory))
    result = json.loads((directory / "operator-1.result.json").read_text())
    assert result["status_code"] == 200 and result["status"] == "reviewed"
    event = server._read_events()[-1]
    assert event["action_id"] == "operator-1" and event["note"] == body["note"]
    before = server.EVENTS_FILE.read_bytes()
    asyncio.run(server.process_alarm_actions(directory))
    assert server.EVENTS_FILE.read_bytes() == before


def test_file_close_keeps_note_and_recurrence_can_raise(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    import server

    body, snapshot = evidence_alarm
    snapshot["alarm_items"] = []
    server.observe_alarms(server._alarm_memory, "state", [], [], STAMP + 60)
    command = tmp_path / "close-1.json"
    command.write_text(
        json.dumps(
            {
                "action": "retire",
                "source": "state",
                "key": KEY,
                "since": STAMP,
                "unchecked_since": STAMP + 60,
                "note": "Replaced drive",
            }
        )
    )
    asyncio.run(server.process_alarm_actions(tmp_path))
    assert KEY not in server._alarm_memory["state"]["alarms"]
    event = server._read_events()[-1]
    assert (event["event"], event["note"], event["action_id"]) == (
        "retired",
        "Replaced drive",
        "close-1",
    )
    events = server.observe_alarms(
        server._alarm_memory,
        "state",
        [{"key": KEY, "text": "new failure"}],
        ["journal"],
        STAMP + 120,
    )
    assert events[0]["event"] == "raised"


def test_bad_file_commands_get_receipts_without_changing_state(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    import server

    before = json.dumps(server._alarm_memory)
    (tmp_path / "bad.json").write_text("not JSON")
    (tmp_path / "unknown.json").write_text(json.dumps({"action": "destroy"}))
    (tmp_path / "symlink.json").symlink_to(tmp_path / "bad.json")
    asyncio.run(server.process_alarm_actions(tmp_path))
    for name in ("bad", "unknown", "symlink"):
        assert (
            json.loads((tmp_path / f"{name}.result.json").read_text())["status_code"]
            == 400
        )
    assert json.dumps(server._alarm_memory) == before


def test_concurrent_review_requests_record_one_operator_action(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    body, _ = evidence_alarm

    async def both() -> list[int]:
        results = await asyncio.gather(
            server.review_alarm(request(body)), server.review_alarm(request(body))
        )
        return sorted(response.status_code for response in results)

    assert asyncio.run(both()) == [200, 409]
    assert [row["event"] for row in server._read_events()] == ["raised", "reviewed"]
