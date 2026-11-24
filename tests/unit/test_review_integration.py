"""Transitions across sensor normalization, alarm memory and API snapshots."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest


def test_aggregate_dependencies_survive_partial_reports_and_restart(
    tmp_path: Path,
) -> None:
    import server

    item = {"key": "sum", "text": "aggregate fault", "requires": ["a", "b"]}
    known = server.track_alarms({}, [item], [], 1000, 3)[0]
    known = server.track_alarms(known, [{**item, "requires": ["b"]}], ["b"], 1030, 3)[0]
    assert known["sum"]["requires"] == ["a", "b"]
    path = tmp_path / "alarms.json"
    server._save_alarm_memory({"state": {"ts": 1030, "alarms": known}}, path)
    known = server._load_alarm_memory(path)["state"]["alarms"]
    for at in (1060, 1090, 1120):
        known, events = server.track_alarms(known, [], ["b"], at, 3)
        assert not events
    assert known["sum"]["since"] == 1000
    for at in (1150, 1180, 1210):
        known, events = server.track_alarms(known, [], ["a", "b"], at, 3)
    assert known == {} and events[0]["event"] == "cleared"


def test_hiding_a_fan_retires_its_rule_and_remembered_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    identity = "@pci0000:00/0000:01:00.0/example/fan1"
    monkeypatch.setattr(server, "TEMP_LABELS", {identity: "CPU fan"})
    monkeypatch.setattr(server, "_SENSOR_HIDE", [re.compile("CPU fan")])
    monkeypatch.setitem(server._CONFIG, "fans", {identity: {"min_rpm": 300}})
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: [])
    monkeypatch.setattr(
        server.hwmon,
        "read_sensors",
        lambda: [
            {
                "id": identity,
                "label": "example fan1",
                "type": server.hwmon.FAN,
                "value": 900,
            }
        ],
    )
    key = f"fan/{identity}"
    known = server.track_alarms({}, [{"key": key, "text": "fan stopped"}], [], 1000, 3)[
        0
    ]
    monkeypatch.setattr(server, "_alarm_memory", {"dashboard": {"alarms": known}})
    result = server.get_hw_sensors()
    assert result["rows"] == [] and result["alarm_items"] == []
    assert key in result["checked"]
    for at in (1030, 1060, 1090):
        known, events = server.track_alarms(known, [], result["checked"], at, 3)
    assert known == {} and events[0]["event"] == "cleared"


def test_hidden_missing_fan_recognizes_legacy_display_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    identity = "@pci0000:00/0000:01:00.0/example/fan1"
    monkeypatch.setattr(server, "TEMP_LABELS", {"example fan1": "CPU fan"})
    monkeypatch.setattr(server, "_SENSOR_HIDE", [re.compile("CPU fan")])
    monkeypatch.setitem(server._CONFIG, "fans", {identity: {"min_rpm": 300}})
    monkeypatch.setattr(server.hwmon, "read_sensors", lambda: [])
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: [])
    key = f"fan/{identity}"
    monkeypatch.setattr(
        server,
        "_alarm_memory",
        {
            "dashboard": {
                "alarms": {key: {"key": key, "text": "fan stopped", "since": 1000}}
            },
        },
    )
    result = server.get_hw_sensors()
    assert result["alarm_items"] == [] and key in result["checked"]


@pytest.mark.parametrize("marked", [True, False])
def test_partial_psu_inputs_never_become_a_total(marked: bool) -> None:
    import server

    rows: list[dict[str, Any]] = [
        {"label": "PS1 Input Power", "value": 100, "unit": "W", "psu_input": True},
        {"label": "PS2 Input Power", "value": None, "unit": None, "psu_input": marked},
        {"label": "CPU", "value": 50, "unit": "W", "total": False},
    ]
    assert server.collector_power_rows({"power": rows}) == [
        {"label": "CPU", "value": 50, "total": False}
    ]
    rows[1].update(value=0, unit="W", psu_input=True)
    assert server.collector_power_rows({"power": rows})[-1] == {
        "label": "PSU input",
        "value": 100,
        "total": True,
    }
    assert server.collector_power_rows(
        {
            "power": rows,
            "alarm_items": [{"key": "capture/power", "text": "timeout"}],
        }
    ) == [{"label": "CPU", "value": 50, "total": False}]
    rows.append({"label": "System Power", "value": 200, "unit": "W", "total": True})
    assert [r["label"] for r in server.collector_power_rows({"power": rows})] == [
        "CPU",
        "System Power",
    ]


def test_apis_serve_pending_and_cached_checks_without_running_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostcheck
    import server

    def unexpected() -> Any:
        raise AssertionError("an HTTP request refreshed expensive checks")

    monkeypatch.setattr(server, "_host_findings", unexpected)
    monkeypatch.setattr(server, "get_zfs", unexpected)
    monkeypatch.setattr(
        server, "_get_storage_health", lambda *args: {"status": "missing"}
    )
    monkeypatch.setattr(server, "_read_events", lambda: [])
    monkeypatch.setattr(
        server,
        "get_hw_sensors",
        lambda: {"alarm_items": [], "checked": [], "notes": []},
    )
    response = json.loads(bytes(asyncio.run(server.storage_health()).body))
    assert (
        response["sensor_status"] == "pending"
        and response["zfs"]["status"] == "pending"
    )
    now = time.monotonic()
    alarm = {"key": "zfs/pool/state", "text": "pool degraded"}
    server._host_cache.update(at=now, data=hostcheck.Findings())
    server._zfs_cache.update(
        at=now, data=(hostcheck.Findings([alarm]), {"status": "ok", "pools": []})
    )
    response = json.loads(bytes(asyncio.run(server.alarms()).body))
    assert response["sources"]["dashboard"]["status"] == "ok"
    assert response["active"][0]["key"] == alarm["key"]
    server._zfs_cache["at"] = now - 4 * server.ZFS_CACHE_SEC
    response = json.loads(bytes(asyncio.run(server.storage_health()).body))
    assert response["sensor_status"] == "stale" and response["zfs"]["status"] == "stale"
    assert response["sensor_alarms"] == [alarm["text"]]


def test_mismatch_deduplication_preserves_collector_evidence_without_host_inference() -> (
    None
):
    import server

    mismatch = {"key": "gpu/driver-mismatch", "text": "NVML mismatch"}
    sensors = {
        "schema": 1,
        "ts": 1000,
        "status": "ok",
        "alarm_items": [mismatch],
        "checked": [],
    }
    services = {"stale": False, "targets": []}
    result = server.alarms_snapshot(
        1000, {"status": "missing"}, sensors, [], services, {}, []
    )
    assert [r["source"] for r in result["active"]] == ["sensors"]
    result = server.alarms_snapshot(
        1000,
        {"status": "missing"},
        sensors,
        [{**mismatch, "text": "driver update requires reboot"}],
        services,
        {},
        [],
    )
    assert [r["source"] for r in result["active"]] == ["dashboard"]
