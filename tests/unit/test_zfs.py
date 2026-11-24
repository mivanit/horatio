"""ZFS pool alarms. Fixtures model OpenZFS 2.4.1 `zpool status -j --json-int`
output; they are synthetic, not captures from a live pool."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT

FIXTURES: Path = ROOT / "tests" / "fixtures" / "zfs"
DAY: float = 86400.0
# healthy.json's last scrub ended at this epoch.
SCRUBBED: float = 1795162800.0


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


class zpool:  # noqa: N801 - reads as the fake command it is
    """A fake run_zpool that answers one way and records its calls."""

    def __init__(self, stdout: str = "", rc: int = 0, stderr: str = "") -> None:
        self.reply: tuple[int, str, str] = (rc, stdout, stderr)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        return self.reply


def check(
    run: Any,
    *,
    now: float = SCRUBBED + DAY,
    pools: Any = None,
    seen: Path | None = None,
    labels: set[str] | None = None,
    installed: bool = True,
    loaded: bool = True,
    max_days: Any = None,
) -> tuple[Any, dict[str, Any]]:
    import zfs

    return zfs.zfs_findings(
        pools,
        max_days,
        seen,
        now=now,
        run=run,
        labels=lambda: labels or set(),
        zpool="/usr/sbin/zpool" if installed else None,
        module_loaded=loaded,
    )


def keys(found: Any) -> list[str]:
    return [a["key"] for a in found.alarms]


def test_no_zfs_anywhere_is_nothing_at_all() -> None:
    found, summary = check(zpool(), installed=False, loaded=False)
    assert (found.alarms, found.notes, found.checked) == ([], [], [])
    assert summary == {"status": "absent", "pools": []}


def test_a_healthy_recently_scrubbed_pool_is_quiet(tmp_path: Path) -> None:
    run = zpool(fixture("healthy"))
    found, summary = check(run, seen=tmp_path / "seen.json")
    assert found.alarms == [] and found.notes == []
    assert run.calls == [["/usr/sbin/zpool", "status", "-j", "--json-int"]]
    assert {"zfs/unreadable", "zfs/tank/state", "zfs/tank/errors"} <= set(found.checked)
    assert summary["status"] == "ok" and summary["pools"][0]["name"] == "tank"
    # The pool is remembered, so if it later disappears that is an alarm.
    assert json.loads((tmp_path / "seen.json").read_text()) == ["tank"]
    found, _ = check(zpool(fixture("no-pools")), seen=tmp_path / "seen.json")
    assert keys(found) == ["zfs/tank/missing"]


def test_degraded_with_errors_alarms_on_state_and_errors() -> None:
    found, _ = check(zpool(fixture("degraded-errors")), now=SCRUBBED)
    assert keys(found)[:2] == ["zfs/tank/state", "zfs/tank/errors"]
    assert found.alarms[0]["text"] == "zfs tank: pool is DEGRADED"
    assert found.alarms[1]["value"] > 0


def test_never_scrubbed_and_overdue_are_alarms_a_scrub_running_is_a_note() -> None:
    found, _ = check(zpool(fixture("never-scrubbed")))
    assert keys(found) == ["zfs/tank/never-scrubbed"]
    # 38 days by default (Ubuntu scrubs monthly: gaps of up to 35 days).
    found, _ = check(zpool(fixture("healthy")), now=SCRUBBED + 37 * DAY)
    assert found.alarms == []
    found, _ = check(zpool(fixture("healthy")), now=SCRUBBED + 39 * DAY)
    assert keys(found) == ["zfs/tank/scrub-overdue"]
    found, _ = check(zpool(fixture("healthy")), now=SCRUBBED + 11 * DAY, max_days=10)
    assert keys(found) == ["zfs/tank/scrub-overdue"]
    found, _ = check(zpool(fixture("scrubbing")))
    assert found.alarms == []
    assert len(found.notes) == 1 and "scrub in progress" in found.notes[0]


def test_errors_in_log_special_and_cache_vdevs_count_too() -> None:
    import zfs

    pool: dict[str, Any] = json.loads(fixture("healthy"))["pools"]["tank"]
    pool["logs"] = {"slog": {"read_errors": 0, "write_errors": 2, "checksum_errors": 0}}
    pool["l2cache"] = {"cache": {"checksum_errors": 1}}
    assert zfs.vdev_errors(pool) == (3, False)


def test_expected_pools_that_cannot_be_read_alarm_loudly() -> None:
    # zpool missing although the config names a pool.
    found, summary = check(zpool(), installed=False, pools=["tank"])
    assert keys(found) == ["zfs/unreadable"]
    assert summary["status"] == "error"
    # zpool fails (/dev/zfs not readable) and a disk says it belongs to a pool.
    denied = zpool(
        rc=1, stderr="Permission denied the ZFS utilities must be run as root.\n"
    )
    found, _ = check(denied, labels={"tank"})
    assert keys(found) == ["zfs/unreadable"]
    assert "Permission denied" in found.alarms[0]["text"]
    # The pools' own alarms are held, not cleared: zfs/tank is not checked.
    assert "zfs/tank" not in found.checked
    # Garbage on stdout.
    found, _ = check(zpool("not json"), pools=["tank"])
    assert keys(found) == ["zfs/unreadable"]


def test_unreadable_without_any_expected_pool_is_only_a_note() -> None:
    denied = zpool(rc=1, stderr="Permission denied\n")
    found, _ = check(denied)
    assert found.alarms == [] and found.notes == [
        "zfs: cannot read pool state (Permission denied)"
    ]


def test_a_hung_zpool_is_an_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    import zfs

    def hung(args: list[str]) -> tuple[int, str, str]:
        raise zfs.Hung("zpool did not answer within 10 s (a suspended pool?)")

    found, _ = check(hung, pools=["tank"])
    assert keys(found) == ["zfs/unreadable"] and "suspended" in found.alarms[0]["text"]


def test_run_zpool_kills_a_hang_and_never_starts_a_second() -> None:
    import zfs

    sleeper: list[str] = [sys.executable, "-c", "import time; time.sleep(30)"]
    started: float = time.monotonic()
    with pytest.raises(zfs.Hung):
        zfs.run_zpool(sleeper, timeout=0.5)
    assert time.monotonic() - started < 5
    assert zfs.run_zpool([sys.executable, "-c", "print('ok')"]) == (0, "ok\n", "")


def test_old_zpool_without_json_watches_health_and_says_so() -> None:
    replies: list[tuple[int, str, str]] = [
        (2, "", "invalid option 'j'\nusage:\n"),
        (0, "tank\tDEGRADED\nbackup\tONLINE\n", ""),
    ]
    found, _ = check(lambda args: replies.pop(0), pools=["tank", "gone"])
    assert keys(found) == ["zfs/json-unsupported", "zfs/tank/state", "zfs/gone/missing"]


def test_no_pools_and_none_expected_is_a_note() -> None:
    found, _ = check(zpool(fixture("no-pools")))
    assert found.alarms == [] and found.notes == ["zfs: installed, no pools imported"]


def test_lsblk_labels_name_the_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    import zfs

    tree: dict[str, Any] = {
        "blockdevices": [
            {
                "name": "sda",
                "fstype": None,
                "label": None,
                "children": [
                    {"name": "sda1", "fstype": "zfs_member", "label": "tank"},
                ],
            },
            {"name": "sdb", "fstype": "ext4", "label": "data"},
        ]
    }

    class Result:
        stdout: str = json.dumps(tree)

    assert zfs.zfs_member_labels(lambda *a, **k: Result()) == {"tank"}
    assert (
        zfs.zfs_member_labels(lambda *a, **k: (_ for _ in ()).throw(OSError())) == set()
    )


def test_zfs_joins_the_servers_own_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    import hostcheck
    import server

    monkeypatch.setattr(
        server,
        "get_hw_sensors",
        lambda: {"alarm_items": [], "checked": [], "notes": []},
    )
    alarm: dict[str, Any] = {
        "key": "zfs/tank/state",
        "text": "zfs tank: pool is DEGRADED",
    }
    monkeypatch.setattr(
        server,
        "get_zfs",
        lambda: (hostcheck.Findings([alarm], [], ["zfs/tank"]), {"status": "ok"}),
    )
    found: dict[str, Any] = server.server_findings()
    assert found["alarm_items"] == [alarm] and "zfs/tank" in found["checked"]


@pytest.mark.parametrize("initial", ["healthy", "never-scrubbed"])
@pytest.mark.parametrize(
    ("function", "scan_state"),
    [("SCRUB", "SCANNING"), ("SCRUB", "CANCELED"), ("RESILVER", "FINISHED")],
)
def test_unfinished_scrubs_hold_previous_alarms_until_a_scrub_finishes(
    initial: str, function: str, scan_state: str
) -> None:
    import server

    now = SCRUBBED + 50 * DAY
    first, _ = check(zpool(fixture(initial)), now=now)
    key = "zfs/tank/" + ("scrub-overdue" if initial == "healthy" else "never-scrubbed")
    known, _ = server.track_alarms({}, first.alarms, first.checked, now, 3)
    pool = json.loads(fixture("healthy"))
    pool["pools"]["tank"]["scan_stats"].update(function=function, state=scan_state)
    unknown, _ = check(zpool(json.dumps(pool)), now=now)
    for step in range(1, 4):
        known, events = server.track_alarms(
            known, unknown.alarms, unknown.checked, now + step * 60, 3
        )
        assert events == []
    assert known[key]["since"] == now
    assert "unchecked_since" in known[key]
    assert "zfs/tank/state" in unknown.checked
    assert "zfs/tank/errors" in unknown.checked

    pool["pools"]["tank"]["scan_stats"].update(
        function="SCRUB", state="FINISHED", end_time=int(now + 240)
    )
    recovered, _ = check(zpool(json.dumps(pool)), now=now + 240)
    for step in range(4, 7):
        known, events = server.track_alarms(
            known, recovered.alarms, recovered.checked, now + step * 60, 3
        )
    assert key not in known
    assert [(event["event"], event["key"]) for event in events] == [("cleared", key)]


def test_finished_scrub_without_a_completion_time_is_not_checked() -> None:
    pool = json.loads(fixture("healthy"))
    del pool["pools"]["tank"]["scan_stats"]["end_time"]
    found, _ = check(zpool(json.dumps(pool)))
    assert "zfs/tank/scrub-overdue" not in found.checked
    assert "zfs/tank/never-scrubbed" not in found.checked
    assert "completion time unknown" in found.notes[0]


def test_incomplete_error_counters_hold_an_old_fault_but_report_known_errors() -> None:
    import server

    first, _ = check(zpool(fixture("degraded-errors")), now=SCRUBBED)
    known, _ = server.track_alarms({}, first.alarms, first.checked, SCRUBBED, 3)
    raw = json.loads(fixture("healthy"))
    root = raw["pools"]["tank"]["vdevs"]["tank"]
    del root["read_errors"]
    unknown, summary = check(zpool(json.dumps(raw)))
    assert "zfs/tank/errors" not in unknown.checked
    assert summary["pools"][0]["errors"] is None
    for step in range(1, 4):
        known, events = server.track_alarms(
            known, unknown.alarms, unknown.checked, SCRUBBED + step * 60, 3
        )
    assert "zfs/tank/errors" in known
    assert "zfs/tank/state" not in known  # independent healthy state can recover
    assert known["zfs/tank/errors"]["since"] == SCRUBBED
    root["write_errors"] = 2
    positive, _ = check(zpool(json.dumps(raw)))
    assert keys(positive) == ["zfs/tank/errors"]


def test_legacy_pool_inventory_survives_failed_reads_and_later_disappearance(
    tmp_path: Path,
) -> None:
    seen = tmp_path / "seen.json"

    def run_table(table: str, rc: int = 0) -> Any:
        replies = [(2, "", "invalid option 'j'"), (rc, table, "")]
        return lambda args: replies.pop(0)

    found, _ = check(run_table("tank\tONLINE\n"), seen=seen)
    assert keys(found) == ["zfs/json-unsupported"]
    assert json.loads(seen.read_text()) == ["tank"]
    found, summary = check(run_table("", rc=1), seen=seen)
    assert summary["status"] == "error"
    assert "zfs/tank/missing" not in found.checked
    assert json.loads(seen.read_text()) == ["tank"]
    # Every call reloads the file: no in-memory discovery survives this transition.
    found, _ = check(run_table(""), seen=seen)
    assert keys(found) == ["zfs/json-unsupported", "zfs/tank/missing"]
    assert json.loads(seen.read_text()) == ["tank"]


@pytest.mark.parametrize("pool", [None, [], {}, {"state": None}])
def test_malformed_pool_entries_are_unreadable(pool: Any, tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    found, summary = check(
        zpool(json.dumps({"pools": {"tank": pool}})), pools=["tank"], seen=seen
    )
    assert keys(found) == ["zfs/unreadable"]
    assert summary["status"] == "error"
    assert not any(key.startswith("zfs/tank") for key in found.checked)
    assert not seen.exists()


def test_malformed_legacy_output_does_not_check_or_remember_partial_inventory(
    tmp_path: Path,
) -> None:
    replies = [(2, "", "invalid option 'j'"), (0, "tank\tONLINE\nbroken\n", "")]
    seen = tmp_path / "seen.json"
    found, summary = check(lambda args: replies.pop(0), seen=seen)
    assert summary["status"] == "error"
    assert "zfs/unreadable" in keys(found)
    assert not any(key.startswith("zfs/tank") for key in found.checked)
    assert not seen.exists()
