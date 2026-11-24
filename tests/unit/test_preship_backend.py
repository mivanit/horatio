from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from starlette.requests import Request


@pytest.fixture(autouse=True)
def collector_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_app: None
) -> Path:
    import server

    marker = tmp_path / "collectors-disabled"
    monkeypatch.setattr(server, "COLLECTORS_DISABLED_FILE", marker)
    return marker


def test_concurrent_interval_changes_leave_one_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    server.PASSWORD_FILE.write_text("fixture")

    async def allowed(*args: Any) -> None:
        return None

    monkeypatch.setattr(server, "_verify_password_throttled", allowed)
    tasks: list[asyncio.Task[Any]] = []

    async def recorder() -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        await asyncio.Future()

    monkeypatch.setattr(server, "history_recorder", recorder)

    def request(interval: int) -> Request:
        async def receive() -> Any:
            return {
                "type": "http.request",
                "body": json.dumps({"record_interval": interval}).encode(),
                "more_body": False,
            }

        return Request({"type": "http", "client": ("127.0.0.1", 1234)}, receive)

    async def run() -> None:
        state = server.APP.state
        for name, value in {
            "history_record_interval": 600,
            "history_sample_duration": 10,
            "history_sample_count": 5,
            "manual_record_min_interval": 60,
            "ping_enabled": True,
            "history_processes_enabled": True,
            "history_process_count": 10,
        }.items():
            setattr(state, name, value)
        state.history_task = asyncio.create_task(recorder())
        await asyncio.sleep(0)
        try:
            responses = await asyncio.gather(
                server.update_history_settings(request(700)),
                server.update_history_settings(request(800)),
            )
            await asyncio.sleep(0)
            assert [response.status_code for response in responses] == [200, 200]
            assert [task for task in tasks if not task.done()] == [state.history_task]
            assert state.history_record_interval == 800
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("failed", [True, False])
def test_gpu_processes_never_guess_an_unknown_index(failed: bool) -> None:
    import server

    replies = [
        subprocess.CompletedProcess(
            [], 0, "GPU-A, 123, worker, 64\nGPU-B, 124, worker, 128\n", ""
        ),
        subprocess.CompletedProcess(
            [], 1 if failed else 0, "" if failed else "0, GPU-A\n", ""
        ),
    ]
    with (
        patch.object(server.subprocess, "run", side_effect=replies),
        patch.object(server, "_gpu_process_utilization", return_value={}),
        patch.object(
            server,
            "get_sanitized_process_info",
            return_value={"name": "worker", "path": "worker"},
        ),
    ):
        rows = server._get_gpu_processes()
    assert [(row["pid"], row["gpu_index"]) for row in rows] == (
        [] if failed else [(123, 0)]
    )


def test_cached_host_findings_and_summary_share_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostcheck
    import server

    calls = 0

    def cached() -> Any:
        nonlocal calls
        calls += 1
        return (
            hostcheck.Findings([{"key": "zfs/test/state", "text": str(calls)}]),
            {"status": "ok", "generation": calls},
            "ok",
        )

    monkeypatch.setattr(server, "_cached_host_checks", cached)
    monkeypatch.setattr(
        server,
        "get_hw_sensors",
        lambda: {"alarm_items": [], "checked": [], "notes": []},
    )
    monkeypatch.setattr(server, "_get_storage_health", lambda: {"status": "missing"})
    monkeypatch.setattr(server, "_collector_expected", lambda *args: False)
    result = json.loads(bytes(asyncio.run(server.storage_health()).body))
    assert calls == 1
    assert result["expected"] is False
    assert result["sensor_alarms"] == ["1"] and result["zfs"]["generation"] == 1


def test_optional_collector_is_neutral_only_until_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server

    monkeypatch.setattr(server, "COLLECTOR_UNIT_DIRS", (tmp_path,))
    missing = {"status": "missing"}
    assert not server._collector_expected("state", missing)
    (tmp_path / "horatio-collect.timer").symlink_to("/dev/null")
    assert server._collector_expected("state", missing), (
        "even a masked installed timer is expected"
    )
    assert not server._collector_expected("sensors", missing)
    assert server._collector_expected("sensors", {"status": "error"})
    assert server._collector_expected("sensors", missing), (
        "a disappeared snapshot remains expected"
    )
    server._collectors_seen.clear()
    server._alarm_memory["sensors"] = {"ts": 1, "alarms": {}}
    assert server._collector_expected("sensors", missing), (
        "persisted observations survive restarts"
    )


def test_collector_discovery_failure_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_marker: Path
) -> None:
    import server

    monkeypatch.setattr(server, "COLLECTOR_UNIT_DIRS", (tmp_path,))
    collector_marker.touch()
    with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
        assert server._collector_expected("state", {"status": "missing"})


@pytest.mark.parametrize("source", ["state", "sensors"])
def test_intentional_collector_removal_preserves_memory_and_reinstall_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_marker: Path, source: str
) -> None:
    import server

    monkeypatch.setattr(server, "COLLECTOR_UNIT_DIRS", (tmp_path,))
    missing = {"status": "missing"}
    assert server._collector_expected(source, {"status": "ok"})
    server._alarm_memory[source] = {"ts": 1, "alarms": {"fault": {"text": "held"}}}
    remembered = json.loads(json.dumps(server._alarm_memory))
    assert server._collector_expected(source, missing)

    collector_marker.touch()
    assert not server._collector_expected(source, missing)
    assert server._alarm_memory == remembered and source in server._collectors_seen
    server._collectors_seen.clear()
    assert not server._collector_expected(source, missing), "uninstall survives restart"
    for status in ("ok", "stale", "error"):
        assert server._collector_expected(source, {"status": status})

    unit_name = "horatio-collect" if source == "state" else "horatio-sensors"
    unit = tmp_path / f"{unit_name}.timer"
    unit.symlink_to("/dev/null")
    assert server._collector_expected(source, missing), "installed/masked units win"
    unit.unlink()
    collector_marker.unlink()
    assert server._collector_expected(source, missing), "reinstall restores expectation"
    assert server._alarm_memory == remembered


@pytest.mark.parametrize("kind", ["directory", "symlink", "unreadable"])
def test_invalid_uninstall_marker_does_not_hide_missing_collectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_marker: Path, kind: str
) -> None:
    import server

    monkeypatch.setattr(server, "COLLECTOR_UNIT_DIRS", (tmp_path,))
    if kind == "directory":
        collector_marker.mkdir()
    elif kind == "symlink":
        collector_marker.symlink_to(tmp_path / "missing-target")
    else:
        collector_marker.touch()

        def denied(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "open", denied)
    assert server._collector_expected("state", {"status": "missing"})


def test_sensor_api_exposes_optional_collector_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server

    monkeypatch.setattr(server, "COLLECTOR_UNIT_DIRS", (tmp_path,))
    monkeypatch.setattr(
        server, "_get_storage_health", lambda *args: {"status": "missing"}
    )
    result = json.loads(bytes(asyncio.run(server.sensors_snapshot()).body))
    assert result["status"] == "missing" and result["expected"] is False


def test_mail_cache_checks_permissions_with_unchanged_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    st = SimpleNamespace(
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_dev=1,
        st_ino=1,
        st_size=100,
        st_mode=0o600,
        st_uid=10,
        st_gid=10,
    )
    reads: list[int] = []

    def read() -> dict[str, Any]:
        reads.append(st.st_mode)
        return {"status": "ok", "count": len(reads)}

    monkeypatch.setattr(server, "_get_mail", read)
    with patch.object(server.os, "stat", return_value=st):
        assert server._get_mail_cached()["count"] == 1
        for count, name in enumerate(
            ("st_mode", "st_uid", "st_gid", "st_dev"), start=2
        ):
            setattr(st, name, getattr(st, name) + 1)
            assert server._get_mail_cached()["count"] == count
            assert server._get_mail_cached()["count"] == count


def test_spare_inventory_has_no_counters_but_active_spares_do() -> None:
    import zfs

    healthy = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0}
    root: dict[str, Any] = dict(healthy)
    pool: dict[str, Any] = {
        "vdevs": {"root": root},
        "spares": {"idle": {"state": "AVAIL"}},
    }
    assert zfs.vdev_errors(pool) == (0, True)
    active = {**healthy, "checksum_errors": 2}
    root["vdevs"] = {"active-spare": active}
    assert zfs.vdev_errors(pool) == (2, True)
    del active["read_errors"]
    assert zfs.vdev_errors(pool) == (2, False)
