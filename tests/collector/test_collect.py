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
    "uptime": "3600.25 7200.50\n",
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
    return state, done.returncode, done.stderr


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
        "journal: 1 error-level messages (1 distinct) in 24h, latest: service exit signal",
    ]


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
