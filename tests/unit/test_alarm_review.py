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
    request: pytest.FixtureRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    import server

    monkeypatch.setattr(server, "PBKDF2_ITERATIONS", 1000)
    server.PASSWORD_FILE.write_text(server._hash_password("correct"))
    key = getattr(request, "param", KEY)
    item = {"key": key, "text": "recorded fault", "evidence": ["a", "b"]}
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
        "key": key,
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


@pytest.mark.parametrize("evidence", [["a", "b"], ["b"], ["b", "c"]])
def test_hidden_review_survives_restart_and_checks_returning_evidence(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]], evidence: list[str]
) -> None:
    import server

    body, snapshot = evidence_alarm
    assert asyncio.run(server.review_alarm(request(body))).status_code == 200
    memory = server._load_alarm_memory()
    reviewed = memory["state"]["alarms"][KEY].copy()
    # Positive hidden presence also resets earlier missing/unchecked readings.
    known = {KEY: reviewed | {"absent": 1, "unchecked_since": STAMP, "unchecked": 1}}
    known, events = server.track_alarms(
        known, [], ["journal"], STAMP + 60, 3, hidden=[KEY]
    )
    assert known[KEY] == reviewed
    assert events == []
    memory["state"]["alarms"] = known
    server._save_alarm_memory(memory)
    memory = server._load_alarm_memory()
    assert memory["state"]["alarms"][KEY] == reviewed

    item = snapshot["alarm_items"][0] | {"evidence": evidence}
    new_evidence = "c" in evidence
    # New evidence must be visible before the next tracker pass, too.
    row = server.alarm_rows("state", [item], memory)[0]
    assert ("reviewed" in row) is not new_evidence
    known, events = server.track_alarms(
        memory["state"]["alarms"], [item], ["journal"], STAMP + 120, 3
    )
    assert known[KEY]["since"] == STAMP
    if new_evidence:
        assert "reviewed" not in known[KEY]
        assert [(event["event"], event["reason"]) for event in events] == [
            ("reopened", "new_evidence")
        ]
    else:
        assert known[KEY]["reviewed"] == reviewed["reviewed"]
        assert events == []


def test_hidden_unreviewed_evidence_does_not_generate_a_change_on_return(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    _, snapshot = evidence_alarm
    known, events = server.track_alarms(
        server._alarm_memory["state"]["alarms"],
        [],
        ["journal"],
        STAMP + 60,
        3,
        hidden=[KEY],
    )
    assert events == []
    known, events = server.track_alarms(
        known, snapshot["alarm_items"], ["journal"], STAMP + 120, 3
    )
    assert events == []
    assert "reviewed" not in known[KEY]


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


@pytest.mark.parametrize(
    "evidence_alarm", [KEY, "sel/memory", "sel/hardware"], indirect=True
)
@pytest.mark.parametrize("checked_absent", [False, True])
@pytest.mark.parametrize("evidence", [["a", "b"], ["b"], ["b", "c"]])
def test_retained_review_survives_restart_and_checks_returning_evidence(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
    checked_absent: bool,
    evidence: list[str],
) -> None:
    import server

    body, snapshot = evidence_alarm
    key = body["key"]
    assert asyncio.run(server.review_alarm(request(body))).status_code == 200
    memory = server._load_alarm_memory()
    original = memory["state"]["alarms"][key].copy()
    known, events = server.track_alarms(
        memory["state"]["alarms"], [], [key] if checked_absent else [], STAMP + 60, 3
    )
    assert events == []
    assert known[key]["reviewed"] == original["reviewed"]
    assert known[key]["evidence"] == original["evidence"]
    memory["state"]["alarms"] = known
    server._save_alarm_memory(memory)
    memory = server._load_alarm_memory()
    if not checked_absent:
        held = server.held_rows("state", None, memory)
        assert len(held) == 1 and held[0]["key"] == key
        assert held[0]["unchecked_since"] == STAMP + 60

    item = snapshot["alarm_items"][0] | {"evidence": evidence}
    known, events = server.track_alarms(
        memory["state"]["alarms"], [item], [key], STAMP + 120, 3
    )
    assert known[key]["since"] == STAMP
    assert "unchecked_since" not in known[key] and "absent" not in known[key]
    if "c" in evidence:
        assert "reviewed" not in known[key]
        assert [(event["event"], event["reason"]) for event in events] == [
            ("reopened", "new_evidence")
        ]
    else:
        assert known[key]["reviewed"] == original["reviewed"]
        assert events == []


def test_unchecked_unreviewed_alarm_keeps_its_evidence_baseline(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    import server

    _, snapshot = evidence_alarm
    known, events = server.track_alarms(
        server._alarm_memory["state"]["alarms"], [], [], STAMP + 60, 1
    )
    assert events == []
    known, events = server.track_alarms(
        known, snapshot["alarm_items"], ["journal"], STAMP + 120, 1
    )
    assert events == []
    assert "reviewed" not in known[KEY]
    item = snapshot["alarm_items"][0] | {"evidence": ["b", "c"]}
    _, events = server.track_alarms(known, [item], ["journal"], STAMP + 180, 1)
    assert [(event["event"], event["reason"]) for event in events] == [
        ("changed", "new_evidence")
    ]


@pytest.mark.parametrize("partial_capture", [False, True])
def test_unknown_evidence_baseline_does_not_claim_new_entries(
    partial_capture: bool,
) -> None:
    import server

    item = {"key": KEY, "text": "journal: disk read failed"}
    known, _ = server.track_alarms(
        None,
        [item | {"evidence": ["a", "b"]}] if partial_capture else [item],
        ["journal"],
        STAMP,
        1,
    )
    if partial_capture:
        known, events = server.track_alarms(known, [item], [], STAMP + 60, 1)
        assert events == []
    # Both an old collector and a partial capture leave no comparison baseline.
    known, events = server.track_alarms(
        known, [item | {"evidence": ["a", "b"]}], ["journal"], STAMP + 120, 1
    )
    assert events == []
    assert known[KEY]["since"] == STAMP
    _, events = server.track_alarms(
        known, [item | {"evidence": ["b", "c"]}], ["journal"], STAMP + 180, 1
    )
    assert [(event["event"], event["reason"]) for event in events] == [
        ("changed", "new_evidence")
    ]


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


def test_nested_json_rejected_without_blocking_later_actions(
    evidence_alarm: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    import server

    body, _ = evidence_alarm
    nested = "[" * 30000 + "0" + "]" * 30000
    assert len(nested) < server.ALARM_ACTION_MAX_BYTES
    (tmp_path / "a-nested.json").write_text(nested)
    body = {key: value for key, value in body.items() if key != "password"}
    (tmp_path / "z-review.json").write_text(json.dumps(body | {"action": "review"}))
    asyncio.run(server.process_alarm_actions(tmp_path))
    rejected = json.loads((tmp_path / "a-nested.result.json").read_text())
    assert rejected["status_code"] == 400
    assert rejected["error"].startswith("Invalid action file:")
    accepted = json.loads((tmp_path / "z-review.result.json").read_text())
    assert accepted["status_code"] == 200 and accepted["status"] == "reviewed"
    before = server.EVENTS_FILE.read_bytes()
    asyncio.run(server.process_alarm_actions(tmp_path))
    assert server.EVENTS_FILE.read_bytes() == before


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
