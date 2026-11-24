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


# AMD FX desktop with Fintek sensors and the proprietary NVIDIA driver (no
# hwmon of its own): f71882fg keeps everything, even `name`, under device/;
# k10temp; fam15h_power.
FINTEK_AMD: dict[str, str] = {
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


def test_fintek_keeps_glances_temperature_names_and_finds_every_fan(
    tmp_path: Path,
) -> None:
    import hwmon

    rows: list[dict[str, Any]] = hwmon.read_sensors(
        tree(tmp_path, {**FINTEK_AMD, **NOUVEAU}) / "hwmon"
    )
    temps: dict[str, dict[str, Any]] = by_type(rows, hwmon.TEMP)
    # The established Glances names: history and
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

    # A two-socket coretemp box: labelled sensors keep their label,
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


def test_missing_fan_uses_the_same_effective_rule_as_a_present_fan() -> None:
    import hwmon

    alias = {"raw": "CPU"}
    cases = [
        ({"default": {"min_rpm": 300}, "raw": {"hide_if_zero": False}}, 300),
        ({"default": {"on_low": "note"}, "raw": {"min_rpm": 300}}, None),
        ({"CPU": {"min_rpm": 300}, "raw": {"min_rpm": 600}}, 600),
        ({"CPU": {"min_rpm": 300}, "raw": {"min_rpm": None}}, None),
    ]
    for rules, minimum in cases:
        _, present, _, read = hwmon.fan_status([fan("raw", 0)], rules, aliases=alias)
        _, absent, _, missing = hwmon.fan_status([], rules, aliases=alias)
        assert read == missing == ["fan/raw"]
        if minimum is None:
            assert present == absent == []
        else:
            assert len(present) == len(absent) == 1
            assert present[0]["key"] == absent[0]["key"] == "fan/raw"
            assert f"{minimum} RPM" in present[0]["text"]
            assert f"min_rpm {minimum}" in absent[0]["text"]


def test_default_only_missing_fan_holds_until_rule_is_disabled() -> None:
    import hwmon
    import server

    identity = "@pci0000:00/0000:01:00.0/amdgpu/fan1"
    row = {**fan("amdgpu fan1", 0), "id": identity}
    rules = {"default": {"min_rpm": 300}}
    _, items, _, checked = hwmon.fan_status([row], rules)
    memory, _ = server.track_alarms({}, items, checked, 1000, 3)
    for now in (1030, 1060, 1090):
        _, items, _, checked = hwmon.fan_status([], rules, known={identity})
        memory, events = server.track_alarms(memory, items, checked, now, 3)
        assert events == []
    assert memory[f"fan/{identity}"]["since"] == 1000
    for now in (1120, 1150, 1180):
        _, items, _, checked = hwmon.fan_status([], {}, known={identity})
        memory, events = server.track_alarms(memory, items, checked, now, 3)
    assert memory == {} and events[0]["event"] == "cleared"


def test_device_disappearance_and_reprobe_never_transfer_a_fan_rule(
    tmp_path: Path,
) -> None:
    import hwmon
    import server

    def read(root: Path, devices: list[tuple[str, int]]) -> list[dict[str, Any]]:
        files: dict[str, str] = {}
        links: dict[str, str] = {}
        for number, (device, rpm) in enumerate(devices):
            files[f"devices/{device}/present"] = ""
            files[f"hwmon/hwmon{number}/name"] = "amdgpu"
            files[f"hwmon/hwmon{number}/fan1_input"] = str(rpm)
            links[f"hwmon/hwmon{number}/device"] = f"../../devices/{device}"
        tree(root, files, links)
        return hwmon.read_sensors(root / "hwmon", root / "thermal")

    original = read(tmp_path / "first", [("gpu-a", 0), ("gpu-b", 1000)])
    faulty_id, healthy_id = [r["id"] for r in original]
    assert faulty_id != healthy_id
    rules = {faulty_id: {"min_rpm": 300}}
    _, alarms, _, checked = hwmon.fan_status(original, rules)
    memory, _ = server.track_alarms({}, alarms, checked, 1000, 3)
    survivor = read(tmp_path / "restart", [("gpu-b", 1000)])
    assert survivor[0]["label"] == original[0]["label"]  # old ordinal naming moves
    assert survivor[0]["id"] == healthy_id
    for now in (1030, 1060, 1090):
        _, alarms, _, checked = hwmon.fan_status(survivor, rules)
        memory, events = server.track_alarms(memory, alarms, checked, now, 3)
        assert events == []
    assert memory[f"fan/{faulty_id}"]["since"] == 1000
    # A legacy label is never allowed to select the healthy survivor instead.
    _, unresolved, notes, _ = hwmon.fan_status(
        survivor, {"amdgpu fan1": {"min_rpm": 300}}
    )
    assert len(unresolved) == 1 and "stable id" in unresolved[0]["text"]
    assert unresolved[0]["key"] == "fan/amdgpu fan1"
    assert notes == []


def test_stable_alias_and_spinning_state_follow_physical_fan() -> None:
    import hwmon

    identity = "@platform/fan-controller/controller/fan1"
    row = {**fan("CPU Fan", 0), "id": identity}
    aliases = {identity: "Processor fan"}
    _, alarms, _, checked = hwmon.fan_status(
        [row], {"Processor fan": {"min_rpm": 300}}, aliases=aliases
    )
    assert checked == [f"fan/{identity}"]
    assert alarms == [
        {
            "key": f"fan/{identity}",
            "text": "fan Processor fan: 0 RPM, below its 300 RPM minimum",
        }
    ]
    assert hwmon.fan_status([row], {}, spun={identity})[0] == [row]
    assert hwmon.fan_status([row], {}, spun={"CPU Fan"})[0] == []


def test_class_symlink_identity_ignores_probe_number(tmp_path: Path) -> None:
    import hwmon

    identifiers = []
    for boot, number in (("first", 2), ("second", 9)):
        root = tmp_path / boot
        physical = f"devices/platform/fan-controller/hwmon/hwmon{number}"
        tree(
            root,
            {f"{physical}/name": "controller", f"{physical}/fan1_input": "900"},
            {f"hwmon/hwmon{number}": f"../{physical}"},
        )
        identifiers.append(
            hwmon.read_sensors(root / "hwmon", root / "thermal")[0]["id"]
        )
    assert identifiers == ["@platform/fan-controller/controller/fan1"] * 2


def test_an_ambiguous_alias_never_selects_a_physical_fan() -> None:
    import hwmon

    ids = ["@devices/a/controller/fan1", "@devices/b/controller/fan1"]
    rows = [{**fan("CPU Fan", 1000), "id": identity} for identity in ids]
    aliases = dict.fromkeys(ids, "CPU")
    _, alarms, _, checked = hwmon.fan_status(
        rows, {"CPU": {"min_rpm": 300}}, aliases=aliases
    )
    assert [alarm["key"] for alarm in alarms] == ["fan/CPU"]
    assert "cannot identify" in alarms[0]["text"]
    assert checked == [f"fan/{identity}" for identity in ids] + ["fan/CPU"]


def test_invalid_minimum_is_unknown_but_null_deliberately_disables() -> None:
    import hwmon

    for rows in ([], [fan("cpu", 0)]):
        _, alarms, notes, checked = hwmon.fan_status(rows, {"cpu": {"min_rpm": "300"}})
        assert alarms == [] and "not a number" in notes[0]
        assert checked == []
        _, alarms, _, checked = hwmon.fan_status(rows, {"cpu": {"min_rpm": None}})
        assert alarms == [] and checked == ["fan/cpu"]


def test_usb_hid_identity_uses_host_port_and_interface_not_probe_counters(
    tmp_path: Path,
) -> None:
    import hwmon

    def identity(
        root: Path,
        bus: int,
        instance: str,
        port: int = 2,
        interface: int = 0,
        version: str = "2.00",
    ) -> str | None:
        hub = f"devices/pci0000:00/0000:00:14.0/usb{bus}"
        physical = f"{hub}/{bus}-{port}/{bus}-{port}:1.{interface}/0003:0C70:F00D.{instance}/hwmon/hwmon9"
        tree(
            root,
            {
                f"{hub}/version": version,
                f"{physical}/name": "cooler",
                f"{physical}/fan1_input": "900",
            },
        )
        return hwmon._fan_id(root / physical, "cooler", 1)

    original = identity(tmp_path / "first", 1, "0001")
    assert original is not None
    assert identity(tmp_path / "reboot", 8, "00A0") == original
    assert identity(tmp_path / "other-port", 8, "00A0", port=3) != original
    assert identity(tmp_path / "other-interface", 8, "00A0", interface=1) != original
    assert identity(tmp_path / "other-hub", 8, "00A0", version="3.00") != original


def test_i2c_identity_retains_controller_address_and_mux_channel(
    tmp_path: Path,
) -> None:
    import hwmon

    def identity(
        root: Path,
        bus: int,
        child: int,
        channel: int = 3,
        address: str = "002d",
        port: int = 0,
        link_channel: bool = True,
    ) -> str | None:
        adapter = f"devices/platform/i2c-controller/i2c-{bus}"
        mux = f"{adapter}/{bus}-0070"
        child_bus = f"{mux}/i2c-{child}"
        physical = f"{child_bus}/{child}-{address}/hwmon/hwmon4"
        links = {f"{child_bus}/mux_device": ".."}
        if link_channel:
            links[f"{mux}/channel-{channel}"] = f"i2c-{child}"
        tree(
            root,
            {
                f"{adapter}/name": f"controller port {port}",
                f"{physical}/name": "fancontroller",
                f"{physical}/fan1_input": "900",
            },
            links,
        )
        return hwmon._fan_id(root / physical, "fancontroller", 1)

    original = identity(tmp_path / "first", 7, 50)
    assert original is not None
    assert identity(tmp_path / "reboot", 9, 73) == original
    assert identity(tmp_path / "other-channel", 9, 73, channel=4) != original
    assert identity(tmp_path / "other-address", 9, 73, address="002e") != original
    assert identity(tmp_path / "other-port", 9, 73, port=1) != original
    assert identity(tmp_path / "unknown-channel", 9, 73, link_channel=False) is None


def test_old_history_alias_and_physical_alias_count_as_one_target() -> None:
    import hwmon

    identity = "@platform/controller/controller/fan1"
    row = {**fan("controller fan1", 900), "id": identity}
    aliases = {"controller fan1": "CPU", identity: "CPU"}
    _, alarms, notes, checked = hwmon.fan_status(
        [row], {"CPU": {"min_rpm": 300}}, aliases=aliases
    )
    assert alarms == notes == []
    assert checked == [f"fan/{identity}"]
    # A missing historical label is display-only; the explicit physical alias
    # still selects the configured device, including when it is absent.
    _, alarms, _, checked = hwmon.fan_status(
        [], {"CPU": {"min_rpm": 300}}, aliases=aliases
    )
    assert [alarm["key"] for alarm in alarms] == checked == [f"fan/{identity}"]


def test_history_alias_does_not_override_a_conflicting_physical_target() -> None:
    import hwmon

    first = "@platform/a/controller/fan1"
    second = "@platform/b/controller/fan1"
    aliases = {"controller fan1": "CPU", first: "CPU"}
    _, alarms, _, _ = hwmon.fan_status(
        [{**fan("controller fan1", 900), "id": second}],
        {"CPU": {"min_rpm": 300}},
        aliases=aliases,
    )
    assert [a["key"] for a in alarms] == ["fan/CPU"]
    assert "cannot identify" in alarms[0]["text"]


def test_unidentified_fan_reports_current_low_reading_without_clearing_history() -> (
    None
):
    import hwmon
    import server

    row = {**fan("controller fan1", 0), "id": None}
    legacy = "fan/controller fan1"
    known, _ = server.track_alarms(
        {}, [{"key": legacy, "text": "old fault"}], [legacy], 1000, 3
    )
    for now in (1030, 1060, 1090):
        _, items, notes, checked = hwmon.fan_status(
            [row], {"default": {"min_rpm": 300}}, known={"controller fan1"}
        )
        assert len(items) == 1 and "below its 300 RPM" in items[0]["text"]
        assert "key" not in items[0] and checked == []
        assert "cannot be tracked" in notes[0]
        known, events = server.track_alarms(known, items, checked, now, 3)
        assert events == [] and known[legacy]["since"] == 1000
    _, alarms, notes, _ = hwmon.fan_status(
        [row], {"controller fan1": {"min_rpm": 300, "on_low": "note"}}
    )
    assert alarms == [] and any("below its 300 RPM" in note for note in notes)
    assert not any("choose a stable id" in note for note in notes)


def test_huge_integer_minimum_is_reported_as_invalid_configuration() -> None:
    import hwmon

    huge = 10**1000
    assert hwmon._minimum({"min_rpm": huge}) is None
    _, alarms, notes, checked = hwmon.fan_status(
        [fan("cpu", 0)], {"default": {"min_rpm": huge}}
    )
    assert alarms == [] and checked == []
    assert any("not a number" in note for note in notes)
