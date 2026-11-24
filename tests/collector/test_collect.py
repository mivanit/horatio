#!/usr/bin/env python3
"""Run the collector's real python stage on staged captures and fake sysfs trees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tests.support import ROOT

SOURCE: str = (ROOT / "collector/health-collect.sh").read_text()
PYTHON: str = SOURCE.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]

# What the bash stage writes for a quiet box with no drives and no backup timer.
CAPTURES: dict[str, str] = {
    "tmp/sel.txt": "",
    "tmp/sel.err": "",
    "tmp/lsblk.json": '{"blockdevices": []}',
    "tmp/lsblk.err": "",
    "tmp/failed.txt": "",
    "tmp/failed.err": "",
    "tmp/journal-err.txt": "",
    "tmp/journal-err.err": "",
    "tmp/backup-timer.name": "\n",
    "tmp/backup-timer.txt": "",
    "tmp/backup-timer.err": "",
    "tmp/prev-boot.txt": "",
    "tmp/prev-boot-kernel.txt": "",
    "uptime": "3600.25 7200.50\n",
    "boot_id": "boot-1\n",
    # One healthy DIMM: the default box has ECC reporting (tests about a box
    # without it pass None to drop this file).
    "edac/mc0/dimm0/dimm_label": "DIMM#0\n",
    "config.json": "{}",
    "out/sensors.json": "{}",
}
TODAY: str = time.strftime("%m/%d/%y")


def collect(
    stage: Path, files: dict[str, str | None] | None = None
) -> tuple[dict[str, Any], int, str]:
    """Stage CAPTURES updated by `files` (None deletes one), run the python stage,
    and return (state.json, exit status, stderr)."""
    path: str
    text: str | None
    for path, text in {**CAPTURES, **(files or {})}.items():
        if text is None:
            continue
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        (stage / path).write_text(text)
    (stage / "out").mkdir(exist_ok=True)
    (stage / "edac").mkdir(exist_ok=True)
    (stage / "block").mkdir(exist_ok=True)
    out: Path = stage / "out/state.json"
    env: dict[str, str] = dict(
        os.environ,
        HEALTH_CONFIG=str(stage / "config.json"),
        HEALTH_EDAC=str(stage / "edac"),
        HEALTH_UPTIME=str(stage / "uptime"),
        HEALTH_BOOT_ID=str(stage / "boot_id"),
        HEALTH_SYSBLOCK=str(stage / "block"),
    )
    done: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-", str(stage / "tmp"), str(out)],
        input=PYTHON,
        text=True,
        env=env,
        capture_output=True,
        timeout=30,
    )
    state: dict[str, Any] = json.loads(out.read_text()) if out.exists() else {}
    if state:
        # Every run: alarm_items is `alarms` with a unique key on each line.
        items: list[dict[str, Any]] = state["alarm_items"]
        assert [i["text"] for i in items] == state["alarms"], done.stderr
        assert len({i["key"] for i in items}) == len(items), items
    return state, done.returncode, done.stderr


def covered(state: dict[str, Any], key: str) -> bool:
    """True when `checked` says this run really evaluated the key."""
    return any(
        key == prefix or key.startswith(prefix + "/") for prefix in state["checked"]
    )


def keys(state: dict[str, Any]) -> list[str]:
    return [i["key"] for i in state["alarm_items"]]


def sel_line(id_: str, sensor: str, event: str, direction: str = "Asserted") -> str:
    return f"  {id_} | {TODAY} | 03:04:05 UTC | {sensor} | {event} | {direction}\n"


def smart(stage: Path, name: str, rc: int, report: dict[str, Any]) -> dict[str, str]:
    return {
        f"tmp/smart_{name}.rc": f"{rc}\n",
        f"tmp/smart_{name}.json": json.dumps(report),
        f"tmp/smart_{name}.err": "",
    }


# ---- every section, one finding each -----------------------------------------


def test_quiet_box_has_no_alarms(tmp_path: Path) -> None:
    state: dict[str, Any]
    rc: int
    state, rc, _ = collect(tmp_path)
    assert rc == 0
    assert (state["alarms"], state["info"]) == ([], [])
    assert state["backup"] is None
    assert state["edac"]["uptime_seconds"] == 3600
    assert (state["edac"]["ce_total"], state["edac"]["ue_total"]) == (0, 0)
    # A BMC is present (no marker) and its log is empty: real zeros.
    assert state["sel_memory"] == {"count": 0, "recent": []}
    assert state["sel_hardware"] == {"count": 0, "recent": []}


def test_markers_for_missing_tools(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path, {"tmp/no-smartctl": "", "tmp/no-bmc": ""}
    )[0]
    assert state["alarms"] == ["smartmontools not installed: drives are not monitored"]
    assert state["info"] == ["no BMC (ipmitool or /dev/ipmi0 missing): no event log"]
    # No BMC: no event-log counts at all, not "0 lifetime".
    assert (state["sel_memory"], state["sel_hardware"]) == (None, None)


def test_sel_memory_hardware_and_undated_lines(tmp_path: Path) -> None:
    sel: str = (
        sel_line("1a", "Memory #0x02", "Correctable ECC | DIMMA1")
        + sel_line("1b", "Power Supply #0x63", "Redundancy Lost")
        + sel_line("1c", "Power Supply #0x63", "Redundancy Lost", "Deasserted")
        + "  1d | 2024-01-02 | 03:04:05 UTC | Fan #0x30 | failed | Asserted\n"
    )
    state: dict[str, Any] = collect(tmp_path, {"tmp/sel.txt": sel})[0]
    assert state["sel_memory"]["count"] == 1
    # The undated line counts toward the lifetime total, never as recent.
    assert state["sel_hardware"]["count"] == 2
    assert len(state["sel_hardware"]["recent"]) == 1
    assert [a.split(" in 7d")[0] for a in state["alarms"]] == [
        "SEL: 1 memory events",
        "SEL: 1 hardware events",
        "collector: 1 SEL lines with unrecognised date format, e.g. 1d | 2024-01-02 | 03:04:05 UTC | Fan #0x30 | failed | Asserted",
    ]


def test_sel_fault_phrases_come_from_config(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path,
        {
            "tmp/sel.txt": sel_line("1b", "Power Supply #0x63", "Redundancy Lost"),
            "config.json": '{"sel_fault_patterns": ["AC lost"]}',
        },
    )[0]
    assert state["alarms"] == []


def test_edac_uncorrectable(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path,
        {
            "edac/mc0/dimm0/dimm_label": "CPU_SrcID#0_Ha#0_Chan#0_DIMM#0\n",
            "edac/mc0/dimm0/dimm_ce_count": "3\n",
            "edac/mc0/dimm0/dimm_ue_count": "1\n",
        },
    )[0]
    assert (state["edac"]["ce_total"], state["edac"]["ue_total"]) == (3, 1)
    assert state["alarms"] == ["ECC: 1 uncorrectable errors since boot"]


def test_no_bmc_and_no_ecc_is_two_notes_and_no_counts(tmp_path: Path) -> None:
    # A desktop: neither source exists, so the Hardware card gets no "0 events" rows.
    state: dict[str, Any] = collect(
        tmp_path, {"tmp/no-bmc": "", "edac/mc0/dimm0/dimm_label": None}
    )[0]
    assert (state["sel_memory"], state["sel_hardware"], state["edac"]) == (
        None,
        None,
        None,
    )
    assert state["alarms"] == []
    assert state["info"] == [
        "no BMC (ipmitool or /dev/ipmi0 missing): no event log",
        "no ECC reporting (no DIMMs under EDAC): memory errors are not monitored",
    ]


def test_edac_controller_without_dimms_is_not_monitored(tmp_path: Path) -> None:
    # bodel: /sys/devices/system/edac/mc exists (edac_mce_amd) but holds no DIMMs.
    state: dict[str, Any] = collect(
        tmp_path, {"edac/mc0/dimm0/dimm_label": None, "edac/mc0/ce_count": "0\n"}
    )[0]
    assert state["edac"] is None
    assert state["alarms"] == []
    assert state["info"] == [
        "no ECC reporting (no DIMMs under EDAC): memory errors are not monitored"
    ]


def test_drives_health_standby_baseline_and_holds(tmp_path: Path) -> None:
    lsblk: dict[str, Any] = {
        "blockdevices": [
            {
                "name": "sda",
                "type": "disk",
                "mountpoints": [None],
                "children": [{"name": "sda1", "type": "part", "mountpoints": ["/"]}],
            }
        ]
    }
    files: dict[str, str | None] = {
        "tmp/lsblk.json": json.dumps(lsblk),
        "config.json": '{"grown_defect_baseline": {"S2": 4}}',
        **smart(
            tmp_path,
            "sda",
            0,
            {"serial_number": "S1", "smart_status": {"passed": False}},
        ),
        **smart(
            tmp_path,
            "sdb",
            0,
            {
                "serial_number": "S2",
                "smart_status": {"passed": True},
                "scsi_grown_defect_list": 4,
                "scsi_self_test_0": {},
            },
        ),
        **smart(tmp_path, "sdc", 3, {}),
    }
    state: dict[str, Any] = collect(tmp_path, files)[0]
    assert [d["device"] for d in state["drives"]] == ["sda", "sdb", "sdc"]
    assert state["drives"][0]["holds"] == ["/"]
    assert state["drives"][1]["grown_defect_baseline"] == 4
    assert state["alarms"] == ["sda: SMART health FAILED"]
    assert state["info"] == ["sdc: standby"]


def test_md_degraded(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path,
        {
            "block/md0/md/degraded": "1\n",
            "block/md0/md/sync_action": "idle\n",
            "block/md0/md/array_state": "clean\n",
            "block/md0/md/dev-sda1/state": "in_sync\n",
        },
    )[0]
    assert state["md"][0]["members"] == [
        {"name": "sda1", "state": ["in_sync"], "errors": 0}
    ]
    assert state["alarms"] == ["md0: DEGRADED (1 missing)"]


def test_backup_late_and_never_fired(tmp_path: Path) -> None:
    late: str = str(int(time.time()) - 40 * 3600)
    state: dict[str, Any] = collect(
        tmp_path,
        {"tmp/backup-timer.name": "b.timer\n", "tmp/backup-timer.txt": late},
    )[0]
    assert state["backup"]["timer"] == "b.timer"
    assert state["alarms"] == ["backup: last trigger 40h ago"]
    never: dict[str, str | None] = {
        "tmp/backup-timer.name": "b.timer\n",
        "tmp/backup-timer.txt": "",
    }
    # Up for an hour: only a Persistent=true timer would know its last trigger.
    state = collect(tmp_path, never)[0]
    assert state["alarms"] == []
    assert state["info"] == ["backup: b.timer has not fired since boot (1h ago)"]
    state = collect(tmp_path, {**never, "uptime": "180000.0 1.0\n"})[0]
    assert state["alarms"] == ["backup: b.timer has never fired"]


def test_missing_sensors_snapshot(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(tmp_path, {"out/sensors.json": None})[0]
    assert state["alarms"] == [
        "collector: no sensors snapshot (horatio-sensors.timer never ran?)"
    ]


def test_failed_units_and_journal_errors(tmp_path: Path) -> None:
    journal: str = (
        "2026-11-17T19:57:38+0000 serverus serverus-fans[1]: service exit signal\n"
        "2026-11-17T19:58:00+0000 serverus sudo[2]:   miv : a password is required\n"
    )
    state: dict[str, Any] = collect(
        tmp_path,
        {
            "tmp/failed.txt": "x.service loaded failed failed X\n",
            "tmp/journal-err.txt": journal,
        },
    )[0]
    assert state["failed_units"] == ["x.service"]
    assert state["alarms"] == [
        "unit failed: x.service",
        "journal: serverus-fans: service exit signal (1 in 24h, at 2026-11-17 19:57)",
    ]
    assert state["journal_errors"] == {
        "hours": 24,
        "count": 1,
        "distinct": ["service exit signal"],
    }


def test_err_sidecars_alarm_except_empty_sel(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path,
        {"tmp/sel.err": "SEL has no entries\n", "tmp/lsblk.err": "exit 124\n"},
    )[0]
    assert state["alarms"] == ["collector: lsblk: exit 124"]


# ---- one crashing section costs only itself -----------------------------------


def test_unconverted_backup_date_crashes_only_the_backup_section(
    tmp_path: Path,
) -> None:
    # 2026-11-17 on serverus: systemd 255's date reached int() and no state.json
    # was written for hours, hiding every other alarm with it.
    state: dict[str, Any]
    rc: int
    err: str
    state, rc, err = collect(
        tmp_path,
        {
            "tmp/backup-timer.name": "b.timer\n",
            "tmp/backup-timer.txt": "Tue 2026-11-17 03:10:05 UTC\n",
            "tmp/failed.txt": "x.service loaded failed failed X\n",
        },
    )
    assert rc == 1
    assert "Traceback" in err
    assert state["alarms"] == [
        "collector: backup crashed: ValueError: invalid literal for int() with base 10: 'Tue 2026-11-17 03:10:05 UTC'",
        "unit failed: x.service",
    ]
    assert "backup" not in state
    assert state["failed_units"] == ["x.service"]


def test_impossible_sel_date_crashes_only_the_sel_sections(tmp_path: Path) -> None:
    state: dict[str, Any]
    rc: int
    state, rc, _ = collect(
        tmp_path,
        {
            "tmp/sel.txt": "  1d | 13/45/24 | 03:04:05 UTC | Fan #0x30 | failed | Asserted\n",
            "tmp/failed.txt": "x.service loaded failed failed X\n",
        },
    )
    assert rc == 1
    assert [a.split(": ")[:2] for a in state["alarms"]] == [
        ["collector", "sel_hardware crashed"],
        ["unit failed", "x.service"],
    ]
    assert state["sel_memory"]["count"] == 0


def test_malformed_config_crashes_only_what_needs_it(tmp_path: Path) -> None:
    state: dict[str, Any]
    rc: int
    state, rc, _ = collect(
        tmp_path,
        {
            "config.json": "{not json",
            **smart(
                tmp_path,
                "sda",
                0,
                {
                    "serial_number": "S1",
                    "smart_status": {"passed": True},
                    "scsi_grown_defect_list": 0,
                },
            ),
            "edac/mc0/dimm0/dimm_label": "DIMM#0\n",
            "edac/mc0/dimm0/dimm_ue_count": "1\n",
        },
    )
    assert rc == 1
    assert [a.split(": ")[:2] for a in state["alarms"]] == [
        ["collector", "load_config crashed"],
        ["ECC", "1 uncorrectable errors since boot"],
        ["collector", "smart crashed"],
    ]
    assert [(d["device"], "error" in d) for d in state["drives"]] == [("sda", True)]
    assert state["edac"]["ue_total"] == 1


def test_a_drive_that_crashes_the_check_costs_only_itself(tmp_path: Path) -> None:
    ok: dict[str, Any] = {"serial_number": "S", "smart_status": {"passed": True}}
    state: dict[str, Any]
    rc: int
    state, rc, _ = collect(
        tmp_path,
        {
            **smart(tmp_path, "sda", 0, ok),
            **smart(tmp_path, "sdb", 0, {**ok, "scsi_grown_defect_list": "many"}),
            **smart(tmp_path, "sdc", 0, {**ok, "smart_status": {"passed": False}}),
        },
    )
    assert rc == 1
    assert [(d["device"], "error" in d) for d in state["drives"]] == [
        ("sda", False),
        ("sdb", True),
        ("sdc", False),
    ]
    assert [a.split(": ")[:3] for a in state["alarms"]] == [
        ["collector", "smart crashed", "sdb"],
        ["sdc", "SMART health FAILED"],
    ]


# ---- identity: a stable key per alarm, and what was really checked -----------


def test_each_alarm_has_a_stable_key_and_counters_a_value(tmp_path: Path) -> None:
    state: dict[str, Any] = collect(
        tmp_path,
        {
            "tmp/failed.txt": "x.service loaded failed failed X\n" * 2,
            "tmp/lsblk.err": "exit 124\n",
            "edac/mc0/dimm0/dimm_label": "DIMM#0\n",
            "edac/mc0/dimm0/dimm_ue_count": "2\n",
            **smart(
                tmp_path,
                "sda",
                0,
                {"serial_number": "S1", "smart_status": {"passed": False}},
            ),
        },
    )[0]
    assert (state["schema"], state["boot_id"]) == (1, "boot-1")
    assert state["alarm_items"] == [
        {
            "key": "ecc/uncorrectable",
            "text": "ECC: 2 uncorrectable errors since boot",
            "value": 2,
        },
        {"key": "smart/S1/health", "text": "sda: SMART health FAILED"},
        {"key": "unit/x.service", "text": "unit failed: x.service"},
        # The same key twice in one run stays unique.
        {"key": "unit/x.service#2", "text": "unit failed: x.service"},
        {"key": "capture/lsblk", "text": "collector: lsblk: exit 124"},
    ]


def test_a_drive_keeps_its_key_when_its_letter_moves(tmp_path: Path) -> None:
    report: dict[str, Any] = {"serial_number": "S1", "smart_status": {"passed": False}}
    first: dict[str, Any] = collect(
        tmp_path / "a", {**smart(tmp_path, "sda", 0, report)}
    )[0]
    second: dict[str, Any] = collect(
        tmp_path / "b", {**smart(tmp_path, "sdb", 0, report)}
    )[0]
    assert keys(first) == keys(second) == ["smart/S1/health"]
    assert (first["alarms"], second["alarms"]) == (
        ["sda: SMART health FAILED"],
        ["sdb: SMART health FAILED"],
    )


def test_only_what_was_really_evaluated_is_checked(tmp_path: Path) -> None:
    ok: dict[str, Any] = {"serial_number": "S1", "smart_status": {"passed": True}}
    quiet: dict[str, Any] = collect(
        tmp_path / "quiet", {**smart(tmp_path, "sda", 0, ok)}
    )[0]
    key: str
    for key in (
        "sel/memory",
        "sel/hardware",
        "ecc/correctable",
        "smart/S1/attrs",
        "smart/dev:sda/unreadable",
        "backup/overdue",
        "unit/x.service",
        "journal",
        "capture/sel",
        "collector/backup",
        "smart-tools",
    ):
        assert covered(quiet, key), key

    # A drive left asleep, a BMC that timed out, a missing BMC, a section that
    # crashed: none of them proves an earlier alarm is gone.
    state: dict[str, Any] = collect(
        tmp_path / "partial",
        {
            **smart(tmp_path, "sda", 0, ok),
            **smart(tmp_path, "sdc", 3, {}),
            "tmp/sel.err": "exit 124\n",
            "tmp/backup-timer.name": "b.timer\n",
            "tmp/backup-timer.txt": "Tue 2026-11-17 03:10:05 UTC\n",
            "tmp/journal-err.err": "exit 1\n",
        },
    )[0]
    assert covered(state, "smart/S1/health")
    for key in (
        "smart/dev:sdc/unreadable",
        "sel/memory",
        "sel/hardware",
        "backup/overdue",
        "collector/backup",
        "journal",
    ):
        assert not covered(state, key), key
    no_bmc: dict[str, Any] = collect(tmp_path / "no-bmc", {"tmp/no-bmc": ""})[0]
    assert not covered(no_bmc, "sel/memory")
    # An uncorrectable count hides the correctable rule; a resync hides mismatch_cnt.
    hidden: dict[str, Any] = collect(
        tmp_path / "hidden",
        {
            "edac/mc0/dimm0/dimm_label": "DIMM#0\n",
            "edac/mc0/dimm0/dimm_ue_count": "1\n",
            "block/md0/md/sync_action": "resync\n",
            "block/md0/md/mismatch_cnt": "8\n",
        },
    )[0]
    assert covered(hidden, "ecc/uncorrectable") and not covered(
        hidden, "ecc/correctable"
    )
    assert covered(hidden, "md/md0/degraded") and not covered(hidden, "md/md0/mismatch")


# ---- how the previous boot ended ----------------------------------------------


def test_boot_record_says_how_the_previous_boot_ended(tmp_path: Path) -> None:
    # A power cut: the previous boot's journal stops mid-sentence.
    state: dict[str, Any] = collect(
        tmp_path / "cut",
        {
            "boot_id": "boot-2\n",
            "tmp/prev-boot.txt": (
                "2026-11-24T01:21:24+00:00 serverus tailscaled[2068]: open-conn-track: timeout\n"
                "2026-11-24T01:21:26+00:00 serverus tailscaled[2068]: wg: handshake failed\n"
            ),
            "tmp/prev-boot-kernel.txt": "Linux version 6.8.0-147-generic (buildd@x) (gcc)\n",
        },
    )[0]
    assert state["boot"]["id"] == state["boot_id"] == "boot-2"
    assert state["boot"]["previous"] == {
        "clean": False,
        "ended": "2026-11-24T01:21:26+00:00",
        "kernel": "6.8.0-147-generic",
    }
    assert state["info"] == [
        "boot: the previous boot ended without a shutdown, last log line 2026-11-24T01:21:26+00:00"
    ]


def test_previous_boot_clean_old_or_unknown(tmp_path: Path) -> None:
    clean: str = (
        "2026-10-22T03:19:17+00:00 serverus systemd-shutdown[1]: Sending SIGTERM to remaining processes...\n"
        "2026-10-22T03:19:17+00:00 serverus systemd-journald[2875594]: Journal stopped\n"
    )
    state: dict[str, Any] = collect(tmp_path / "clean", {"tmp/prev-boot.txt": clean})[0]
    assert state["boot"]["previous"] == {
        "clean": True,
        "ended": "2026-10-22T03:19:17+00:00",
        "kernel": None,
    }
    assert state["info"] == []
    # Unclean, but the box has been up for more than a day: no longer worth a note.
    state = collect(
        tmp_path / "old",
        {
            "tmp/prev-boot.txt": "2026-11-24T01:21:26+00:00 serverus x[1]: last words\n",
            "uptime": "90000.0 1.0\n",
        },
    )[0]
    assert (state["boot"]["previous"]["clean"], state["info"]) == (False, [])
    # A journal that holds only this boot.
    assert collect(tmp_path / "none")[0]["boot"]["previous"] is None


# ---- journal: one alarm per message; drives: one per attribute, with a baseline ----


def journal_line(when: str, unit: str, message: str) -> str:
    return f"2026-11-{when}+00:00 serverus {unit}: {message}\n"


def test_journal_gives_each_distinct_message_its_own_alarm(tmp_path: Path) -> None:
    failsafe: str = (
        "serverus-fans: failsafe, requesting 100 %: ipmitool timed out after {} s"
    )
    journal: str = (
        journal_line("23T15:28:18", "serverus-fans[1275534]", failsafe.format(10))
        + journal_line(
            "24T01:24:03",
            "kernel",
            "NVRM: loading NVIDIA UNIX x86_64 Kernel Module  580.196.07",
        )
        + journal_line(
            "24T01:24:03",
            "kernel",
            "ACPI Error: AE_NOT_EXIST, Returned by Handler for [IPMI]",
        )
        # The same message from another process, with another number in it.
        + journal_line("24T03:00:00", "serverus-fans[2190]", failsafe.format(12))
        + "    a continuation line\n"
    )
    state: dict[str, Any] = collect(tmp_path, {"tmp/journal-err.txt": journal})[0]
    assert state["alarms"] == [
        "journal: serverus-fans: failsafe, requesting 100 %: ipmitool timed out after 12 s (2 in 24h, first 2026-11-23 15:28, last 2026-11-24 03:00)",
        "journal: kernel: NVRM: loading NVIDIA UNIX x86_64 Kernel Module  580.196.07 (1 in 24h, at 2026-11-24 01:24)",
    ]
    assert all(k.startswith("journal/") and len(k) == 16 for k in keys(state))
    assert covered(state, keys(state)[0])
    assert state["journal_errors"]["count"] == 3

    # What is noise on this box only is ignored from the config; a pattern that
    # does not compile is skipped and said so, and the section still runs.
    ignoring: dict[str, Any] = collect(
        tmp_path / "ignore",
        {
            "tmp/journal-err.txt": journal,
            "config.json": json.dumps(
                {"journal_ignore": ["NVRM: loading NVIDIA", "(unclosed"]}
            ),
        },
    )[0]
    assert keys(ignoring) == keys(state)[:1]
    assert ignoring["info"] == [
        "journal_ignore: skipped '(unclosed': missing ), unterminated subpattern at position 0"
    ]
    # The key does not depend on what else is in the journal that day.
    assert (
        keys(
            collect(
                tmp_path / "one", {"tmp/journal-err.txt": journal.splitlines(True)[0]}
            )[0]
        )
        == keys(state)[:1]
    )


def test_journal_shows_at_most_eight_messages_and_then_a_count(tmp_path: Path) -> None:
    journal: str = "".join(
        journal_line(f"24T01:{n:02d}:00", "app[1]", f"error {'abcdefghijk'[n]}")
        for n in range(11)
    )
    state: dict[str, Any] = collect(tmp_path, {"tmp/journal-err.txt": journal})[0]
    assert len(state["alarms"]) == 9
    assert state["alarms"][0].startswith("journal: app: error a (1 in 24h")
    assert state["alarms"][-1] == "journal: 3 more distinct error-level messages in 24h"
    assert keys(state)[-1] == "journal-more"
    # A message that is not shown is not known to be gone.
    assert not covered(state, keys(state)[0])
    fewer: dict[str, Any] = collect(tmp_path, {"tmp/journal-err.txt": journal[:200]})[0]
    assert covered(fewer, keys(state)[0]) and covered(fewer, "journal-more")


def ata(reallocated: int, pending: int = 0) -> dict[str, Any]:
    return {
        "serial_number": "S1",
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "raw": {"value": reallocated}},
                {"id": 197, "raw": {"value": pending}},
                {"id": 199, "raw": {"value": 4}},
            ]
        },
    }


def test_each_ata_attribute_is_its_own_alarm_with_its_own_baseline(
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = collect(tmp_path, {**smart(tmp_path, "sde", 0, ata(8, 2))})[
        0
    ]
    assert state["alarm_items"] == [
        {"key": "smart/S1/reallocated", "text": "sde: reallocated=8", "value": 8},
        {"key": "smart/S1/pending", "text": "sde: pending=2", "value": 2},
    ]
    assert state["drives"][0]["attrs"] == {
        "reallocated": 8,
        "pending": 2,
        "udma_crc": 4,
    }

    def with_baseline(report: dict[str, Any], baseline: Any) -> dict[str, Any]:
        return collect(
            tmp_path,
            {
                **smart(tmp_path, "sde", 0, report),
                "config.json": json.dumps({"smart_baseline": baseline}),
            },
        )[0]

    # Eight remapped sectors accepted for this drive: quiet.
    state = with_baseline(ata(8), {"S1": {"reallocated": 8}})
    assert state["alarms"] == []
    assert state["drives"][0]["attrs_baseline"] == {"reallocated": 8}
    # One more, and it is back, saying what it is measured against.
    state = with_baseline(ata(9), {"S1": {"reallocated": 8}})
    assert state["alarms"] == ["sde: reallocated=9 (baseline 8)"]
    # A baseline for another drive, or one that is not a number, accepts nothing.
    assert with_baseline(ata(8), {"S2": {"reallocated": 8}})["alarms"] == [
        "sde: reallocated=8"
    ]
    assert with_baseline(ata(8), {"S1": {"reallocated": "8"}})["alarms"] == [
        "sde: reallocated=8"
    ]
    assert with_baseline(ata(8), {"S1": 8})["alarms"] == ["sde: reallocated=8"]
