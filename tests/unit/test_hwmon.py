"""hwmon.read_sensors on fake sysfs trees modelled on real boxes, and the fan rules."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def tree(
    root: Path, files: dict[str, str], links: dict[str, str] | None = None
) -> Path:
    """Write a fake /sys/class/hwmon (and friends) under `root`."""
    path: str
    text: str
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text + "\n")
    target: str
    for path, target in (links or {}).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, root / path)
    return root


def by_type(rows: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {r["label"]: r for r in rows if r["type"] == kind}


# bodel (MSI 970A, FX-6300) as it is now, on the proprietary NVIDIA driver (no
# hwmon of its own): f71882fg keeps everything, even `name`, under device/;
# k10temp; fam15h_power.
BODEL: dict[str, str] = {
    "hwmon/hwmon0/device/name": "f71889ed",
    "hwmon/hwmon0/device/temp1_input": "31000",
    "hwmon/hwmon0/device/temp1_max": "85000",
    "hwmon/hwmon0/device/temp1_crit": "80000",
    "hwmon/hwmon0/device/temp2_input": "38000",
    "hwmon/hwmon0/device/temp3_input": "35000",
    "hwmon/hwmon0/device/fan1_input": "1642",
    "hwmon/hwmon0/device/fan2_input": "883",
    "hwmon/hwmon0/device/fan3_input": "0",
    "hwmon/hwmon1/name": "k10temp",
    "hwmon/hwmon1/temp1_input": "19800",
    "hwmon/hwmon1/temp1_max": "70000",
    "hwmon/hwmon1/temp1_crit": "80000",
    "hwmon/hwmon2/name": "fam15h_power",
    "hwmon/hwmon2/power1_input": "53362689",
    "hwmon/hwmon2/power1_crit": "94992187",
}
# Earlier, on nouveau: a GPU chip with a fan in its own hwmon directory, which made
# psutil drop the three device/ fans.
NOUVEAU: dict[str, str] = {
    "hwmon/hwmon3/name": "nouveau",
    "hwmon/hwmon3/temp1_input": "36000",
    "hwmon/hwmon3/fan1_input": "1519",
}


def test_bodel_keeps_glances_temperature_names_and_finds_every_fan(
    tmp_path: Path,
) -> None:
    import hwmon

    rows: list[dict[str, Any]] = hwmon.read_sensors(
        tree(tmp_path, {**BODEL, **NOUVEAU}) / "hwmon"
    )
    temps: dict[str, dict[str, Any]] = by_type(rows, hwmon.TEMP)
    # Exactly what Glances called them, checked against the live box: history and
    # temp_labels keep working.
    assert sorted(temps) == [
        "f71889ed 0",
        "f71889ed 1",
        "f71889ed 2",
        "k10temp 0",
        "nouveau 0",
    ]
    # The BIOS's backwards limits (max 85 > crit 80) come out in order.
    assert (temps["f71889ed 0"]["warning"], temps["f71889ed 0"]["critical"]) == (80, 85)
    # psutil dropped the three device/ fans because nouveau has one of its own.
    assert sorted(by_type(rows, hwmon.FAN)) == [
        "f71889ed fan1",
        "f71889ed fan2",
        "f71889ed fan3",
        "nouveau fan1",
    ]
    power: dict[str, Any] = by_type(rows, hwmon.POWER)["CPU (est.)"]
    assert (power["value"], power["total"], power["unit"]) == (53.4, False, "W")
    assert round(power["critical"]) == 95


def test_glances_naming_for_labels_duplicates_and_sort_order(tmp_path: Path) -> None:
    import hwmon

    # A two-socket coretemp box (serverus-like): labelled sensors keep their label,
    # a repeated label gets " 1", " 2"... counted per chip, as Glances does. hwmon10
    # sorts before hwmon2 (psutil sorts path strings).
    files: dict[str, str] = {
        "hwmon/hwmon2/name": "coretemp",
        "hwmon/hwmon2/temp1_input": "40000",
        "hwmon/hwmon2/temp1_label": "Package id 0",
        "hwmon/hwmon2/temp2_input": "41000",
        "hwmon/hwmon2/temp2_label": "Core 0",
        "hwmon/hwmon10/name": "coretemp",
        "hwmon/hwmon10/temp1_input": "42000",
        "hwmon/hwmon10/temp1_label": "Package id 1",
        "hwmon/hwmon10/temp2_input": "43000",
        "hwmon/hwmon10/temp2_label": "Core 0",
        "hwmon/hwmon10/temp10_input": "44000",
        "hwmon/hwmon10/temp10_label": "Core 9",
    }
    rows: list[dict[str, Any]] = hwmon.read_sensors(tree(tmp_path, files) / "hwmon")
    # Names from psutil's walk (hwmon10 first, so its Core 0 is the plain one),
    # then Glances' natural sort by label.
    assert [(r["label"], r["value"]) for r in rows] == [
        ("Core 0", 43.0),
        ("Core 0 1", 41.0),
        ("Core 9", 44.0),
        ("Package id 0", 40.0),
        ("Package id 1", 42.0),
    ]


def test_junk_inputs_are_dropped_without_renaming_the_rest(tmp_path: Path) -> None:
    import hwmon

    files: dict[str, str] = {
        "hwmon/hwmon0/name": "nct6775",
        "hwmon/hwmon0/temp1_input": "-128000",  # an unconnected AUXTIN pin
        "hwmon/hwmon0/temp2_input": "30000",
        "hwmon/hwmon0/temp2_fault": "1",
        "hwmon/hwmon0/temp3_input": "45000",
        "hwmon/hwmon0/temp4_input": "46000",
        "hwmon/hwmon0/temp4_enable": "0",
        "hwmon/hwmon0/temp5_crit": "90000",  # no _input: skipped, as psutil does
    }
    rows: list[dict[str, Any]] = hwmon.read_sensors(tree(tmp_path, files) / "hwmon")
    # nct6775 3 keeps the name Glances gave it (index 2), though 0 and 1 are gone.
    assert [(r["label"], r["value"]) for r in rows] == [("nct6775 2", 45.0)]


def test_laptop_battery_is_not_power_and_names_never_collide(tmp_path: Path) -> None:
    import hwmon

    files: dict[str, str] = {
        "hwmon/hwmon0/name": "BAT0",
        "hwmon/hwmon0/power1_input": "9000000",  # a charge rate, not system draw
        "power_supply/BAT0/uevent": "",
        "hwmon/hwmon1/name": "thinkpad",
        "hwmon/hwmon1/fan1_input": "2900",
        "hwmon/hwmon1/fan1_label": "CPU Fan",
        "hwmon/hwmon2/name": "thinkpad",
        "hwmon/hwmon2/fan1_input": "2000",
        "hwmon/hwmon2/fan1_label": "CPU Fan",
        "hwmon/hwmon3/name": "acpi_power_meter",
        "hwmon/hwmon3/power1_average": "230000000",  # only an average, no _input
    }
    root: Path = tree(
        tmp_path,
        files,
        links={
            "hwmon/hwmon0/device": "../../power_supply/BAT0",
            "power_supply/BAT0/subsystem": "../../power_supply",
        },
    )
    rows: list[dict[str, Any]] = hwmon.read_sensors(root / "hwmon")
    assert sorted(by_type(rows, hwmon.FAN)) == ["CPU Fan", "CPU Fan 2"]
    power: dict[str, dict[str, Any]] = by_type(rows, hwmon.POWER)
    assert list(power) == ["System (ACPI)"]
    assert (power["System (ACPI)"]["value"], power["System (ACPI)"]["total"]) == (
        230.0,
        True,
    )


def test_thermal_zones_only_when_hwmon_has_no_temperatures(tmp_path: Path) -> None:
    import hwmon

    files: dict[str, str] = {
        "thermal/thermal_zone0/type": "cpu-thermal",
        "thermal/thermal_zone0/temp": "52500",
        "thermal/thermal_zone0/trip_point_0_type": "critical",
        "thermal/thermal_zone0/trip_point_0_temp": "110000",
        "hwmon/hwmon0/name": "pwmfan",  # a Pi 5 Active Cooler
        "hwmon/hwmon0/fan1_input": "3200",
    }
    root: Path = tree(tmp_path, files)
    rows: list[dict[str, Any]] = hwmon.read_sensors(root / "hwmon", root / "thermal")
    temp: dict[str, Any] = by_type(rows, hwmon.TEMP)["cpu-thermal 0"]
    assert (temp["value"], temp["critical"]) == (52.5, 110.0)
    assert list(by_type(rows, hwmon.FAN)) == ["pwmfan fan1"]


def test_a_missing_tree_and_unreadable_files_cost_nothing(tmp_path: Path) -> None:
    import hwmon

    assert hwmon.read_sensors(tmp_path / "nothing", tmp_path / "nothing") == []
    files: dict[str, str] = {
        "hwmon/hwmon0/name": "it87",
        "hwmon/hwmon0/fan1_input": "not a number",
        "hwmon/hwmon0/fan2_input": "1200",
    }
    # The fixture has no temperatures, so the thermal-zone fallback applies: point
    # it into the fixture too, or it reads the host's own /sys/class/thermal.
    root: Path = tree(tmp_path, files)
    rows: list[dict[str, Any]] = hwmon.read_sensors(root / "hwmon", root / "thermal")
    assert [r["label"] for r in rows] == ["it87 fan2"]


# ---- fan rules -------------------------------------------------------------------


def fan(label: str, rpm: float) -> dict[str, Any]:
    return {"type": "fan_speed", "label": label, "value": rpm, "unit": "R"}


def test_fan_rules_hide_empty_headers_and_alarm_below_a_minimum() -> None:
    import hwmon

    rows: list[dict[str, Any]] = [
        fan("cpu", 0),
        fan("case", 0),
        fan("rear", 900),
        {"type": "temperature_core", "label": "t", "value": 30},
    ]
    config: dict[str, Any] = {
        "cpu": {"min_rpm": 300},
        "rear": {"min_rpm": 1000, "on_low": "note"},
        "gone": {"min_rpm": 100},
    }
    shown, alarms, notes = hwmon.apply_fan_rules(rows, config)
    # The stopped CPU fan stays visible; the empty case header (no minimum) is hidden.
    assert [r["label"] for r in shown] == ["cpu", "rear", "t"]
    # A rule with a minimum for a fan that is not there is an alarm: a fan that
    # cannot be read is not a fan that is fine.
    assert alarms == [
        "fan cpu: 0 RPM, below its 300 RPM minimum",
        "fans config names 'gone', which this box does not have (min_rpm 100)",
    ]
    assert notes == ["fan rear: 900 RPM, below its 1000 RPM minimum"]


def test_fan_rules_default_applies_to_every_unnamed_fan() -> None:
    import hwmon

    rows: list[dict[str, Any]] = [fan("a", 0), fan("b", 50)]
    shown, alarms, _ = hwmon.apply_fan_rules(
        rows, {"default": {"hide_if_zero": False, "min_rpm": 100}}
    )
    assert [r["label"] for r in shown] == ["a", "b"]
    assert len(alarms) == 2
    # No config at all: zero fans hidden, nothing alarms.
    shown, alarms, notes = hwmon.apply_fan_rules(rows, None)
    assert ([r["label"] for r in shown], alarms, notes) == (["b"], [], [])


def test_setup_notes_for_a_vm_and_a_desktop_without_its_super_io_driver(
    tmp_path: Path,
) -> None:
    import hwmon

    tree(tmp_path, {"cpuinfo": "flags\t: fpu sse2 hypervisor", "dmi/chassis_type": "1"})
    assert hwmon.setup_notes([], tmp_path / "dmi", tmp_path / "cpuinfo") == [
        "virtual machine: no hardware sensors to read"
    ]
    tree(tmp_path, {"cpuinfo": "flags\t: fpu sse2", "dmi/chassis_type": "3"})
    temps_only: list[dict[str, Any]] = [{"type": "temperature_core", "label": "x"}]
    notes: list[str] = hwmon.setup_notes(
        temps_only, tmp_path / "dmi", tmp_path / "cpuinfo"
    )
    assert len(notes) == 1 and "sensors-detect" in notes[0]
    with_fan: list[dict[str, Any]] = [*temps_only, fan("f", 900)]
    assert hwmon.setup_notes(with_fan, tmp_path / "dmi", tmp_path / "cpuinfo") == []
    # A laptop (chassis 10) without fan readings is normal: no hint.
    tree(tmp_path, {"dmi/chassis_type": "10"})
    assert hwmon.setup_notes(temps_only, tmp_path / "dmi", tmp_path / "cpuinfo") == []


# ---- server ----------------------------------------------------------------------


def test_server_applies_fan_rules_and_hints_in_one_place(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import asyncio
    import json

    import server

    raw: list[dict[str, Any]] = [fan("f71889ed fan1", 0), fan("f71889ed fan2", 0)]
    monkeypatch.setattr(server.hwmon, "read_sensors", lambda: [dict(r) for r in raw])
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: ["a hint"])
    monkeypatch.setitem(server._CONFIG, "fans", {"f71889ed fan1": {"min_rpm": 300}})
    rows: list[dict[str, Any]] = json.loads(
        bytes(asyncio.run(server.hw_sensors()).body)
    )
    assert [r["label"] for r in rows] == ["f71889ed fan1"]  # fan2: empty header, hidden
    found: dict[str, Any] = server.server_findings()
    assert found["alarms"] == ["fan f71889ed fan1: 0 RPM, below its 300 RPM minimum"]
    assert found["notes"] == ["a hint"]


def test_collector_power_rows_read_new_and_old_snapshots() -> None:
    import server

    new: dict[str, Any] = {
        "power": [
            {"label": "Current 1", "value": 0.6, "unit": "A", "total": False},
            {"label": "Pwr Consumption", "value": 238, "unit": "W", "total": True},
            {"label": "CPU 0", "value": 80, "unit": "W", "total": False},
        ]
    }
    old: dict[str, Any] = {
        "power": [
            {
                "label": "Current 1",
                "value": 0.6,
                "reading": "0.60 Amps",
                "entity": "10.1",
            },
            {
                "label": "Pwr Consumption",
                "value": 238,
                "reading": "238 Watts",
                "entity": "7.1",
            },
            {"label": "CPU 0", "value": 80, "reading": "80 Watts", "entity": "rapl"},
        ]
    }
    expected: list[dict[str, Any]] = [
        {"label": "Pwr Consumption", "value": 238, "total": True},
        {"label": "CPU 0", "value": 80, "total": False},
    ]
    assert server.collector_power_rows(new) == expected
    assert server.collector_power_rows(old) == expected


def test_fan_alarms_are_keyed_by_the_fan_not_by_its_reading() -> None:
    import hwmon

    rules: dict[str, Any] = {
        "cpu": {"min_rpm": 300},
        "case": {"min_rpm": 300, "on_low": "note"},
    }
    items: list[dict[str, Any]]
    slow: list[dict[str, Any]]
    notes: list[str]
    _, items, notes = hwmon.fan_findings([fan("cpu", 0), fan("case", 0)], rules)
    _, slow, _ = hwmon.fan_findings([fan("cpu", 120)], rules)
    assert items == [
        {"key": "fan/cpu", "text": "fan cpu: 0 RPM, below its 300 RPM minimum"}
    ]
    assert [i["key"] for i in slow] == ["fan/cpu"]
    assert slow[0]["text"] != items[0]["text"]
    assert notes[0].startswith("fan case: 0 RPM")
    # apply_fan_rules is the same findings with the alarms as plain text.
    assert hwmon.apply_fan_rules([fan("cpu", 0)], rules)[1] == [items[0]["text"]]
