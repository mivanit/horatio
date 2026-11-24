"""Sensor and power fixes from the 2026-11-24 review: each test failed at a468d9f.
Numbers follow the review's list (dotfiles notes/horatio-review-2026-11-24.md)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.support import ROOT
from tests.unit.test_hwmon import BODEL, by_type, fan, tree


# ---- 2. a rule with min_rpm for a fan that is missing -----------------------------
def test_02_missing_fan_with_min_rpm_is_an_alarm() -> None:
    import hwmon

    _, alarms, notes = hwmon.fan_findings(
        [fan("other", 900)], {"cpu": {"min_rpm": 300}}
    )
    assert [a["key"] for a in alarms] == ["fan/cpu"], (alarms, notes)


def test_02b_missing_fan_without_min_rpm_stays_a_note() -> None:
    import hwmon

    _, alarms, notes = hwmon.fan_findings([], {"cpu": {"hide_if_zero": False}})
    assert alarms == [] and len(notes) == 1


# ---- 3. a fan that stopped after spinning is shown at 0 ---------------------------
def test_03_fan_seen_spinning_then_stopped_is_shown(monkeypatch: Any) -> None:
    import server

    reads: list[list[dict[str, Any]]] = [
        [fan("f71889ed fan1", 1200), fan("f71889ed fan3", 0)],
        [fan("f71889ed fan1", 0), fan("f71889ed fan3", 0)],
    ]
    monkeypatch.setattr(server.hwmon, "read_sensors", lambda: reads.pop(0))
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: [])
    monkeypatch.setitem(server._CONFIG, "fans", {})
    server.get_hw_sensors()
    server._hwmon_cache["at"] = -1e18
    rows = server.get_hw_sensors()["rows"]
    # fan1 spun before: shown at 0 (and so recorded). fan3 never spun: hidden.
    assert [(r["label"], r["value"]) for r in rows] == [("f71889ed fan1", 0)]


# ---- 4. a GPU fan must not count as the board's Super I/O fans --------------------
@pytest.mark.parametrize("chip", ["nouveau", "amdgpu", "radeon"])
def test_04_gpu_fan_does_not_suppress_super_io_hint(tmp_path: Path, chip: str) -> None:
    import hwmon

    tree(tmp_path, {"cpuinfo": "flags\t: fpu sse2", "dmi/chassis_type": "3"})
    rows = [{**fan(f"{chip} fan1", 1500), "chip": chip}]
    notes = hwmon.setup_notes(rows, tmp_path / "dmi", tmp_path / "cpuinfo")
    assert len(notes) == 1 and "sensors-detect" in notes[0]


# ---- 5. a rule under the sensor_labels name -------------------------------------
def test_05_fan_rule_by_sensor_labels_alias(monkeypatch: Any) -> None:
    import server

    monkeypatch.setattr(server.hwmon, "read_sensors", lambda: [fan("f71889ed fan1", 0)])
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: [])
    monkeypatch.setattr(server, "TEMP_LABELS", {"f71889ed fan1": "CPU fan"})
    monkeypatch.setitem(server._CONFIG, "fans", {"CPU fan": {"min_rpm": 300}})
    data = server.get_hw_sensors()
    assert [a["key"] for a in data["alarm_items"]] == ["fan/f71889ed fan1"], data


# ---- 6 and 9. the history recorder ------------------------------------------------
def _record(
    server: Any, hw_rows: list[dict[str, Any]], snapshot: dict[str, Any]
) -> dict[str, Any]:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    class FakeClient(httpx.AsyncClient):
        def __init__(self: Any, **k: Any) -> None:
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(**k)

    def health(path: Any = None, stale: Any = None) -> dict[str, Any]:
        return snapshot if path == server.SENSORS_PATH else {"status": "missing"}

    hw = {"rows": hw_rows, "alarms": [], "alarm_items": [], "checked": [], "notes": []}
    with (
        patch.object(server.httpx, "AsyncClient", FakeClient),
        patch.object(server, "get_gpu_stats", return_value=[]),
        patch.object(server, "_get_ip_info", return_value={}),
        patch.object(server, "get_hw_sensors", return_value=hw),
        patch.object(server, "_get_storage_health", health),
    ):
        st = server.APP.state
        st.ping_enabled = False
        st.history_processes_enabled = False
        st.history_process_count = 10
        st.history_sample_duration = 0
        st.history_sample_count = 1
        return asyncio.run(
            server.collect_history_sample("http://glances.invalid", live=True)
        )


ACPI = {"type": "power", "label": "System (ACPI)", "value": 150.0, "total": True}


def test_06_two_collector_totals_server_picks_the_first_like_the_frontend() -> None:
    import server

    snap = {
        "status": "ok",
        "power": [
            {"label": "Pwr Consumption", "value": 200, "unit": "W", "total": True},
            {"label": "System Power", "value": 199, "unit": "W", "total": True},
        ],
    }
    record = _record(server, [ACPI], snap)
    # app.js: power.find(p => p.total && p.fromCollector) -> the first.
    assert record["power_total"] == "Pwr Consumption", record.get("power")


def test_06b_history_record_lets_the_stack_skip_every_total() -> None:
    import server

    snap = {
        "status": "ok",
        "power": [
            {"label": "Pwr Consumption", "value": 200, "unit": "W", "total": True}
        ],
    }
    hw = [ACPI, {"type": "power", "label": "CPU (est.)", "value": 64.0, "total": False}]
    record = _record(server, hw, snap)
    assert record["power_total"] == "Pwr Consumption"
    # The stacked chart drops only power_total (everTotal); every other key is a part.
    totals = set(record.get("power_totals", [record["power_total"]]))
    parts = sum(v for k, v in record["power"].items() if k not in totals)
    assert parts <= record["power"][record["power_total"]], (
        f"ACPI meter stacked as a part: parts {parts} W > total 200 W: {record['power']}"
    )


@pytest.mark.parametrize("key", ["power", "temps", "fans"])
def test_09_null_sections_in_the_sensors_snapshot(key: str) -> None:
    import server

    record = _record(server, [], {"status": "ok", key: None})
    assert "timestamp" in record


def test_09b_collector_power_rows_null() -> None:
    import server

    assert server.collector_power_rows({"power": None}) == []


def test_09c_storage_health_drives_null() -> None:
    import server

    def health(path: Any = None, stale: Any = None) -> dict[str, Any]:
        return (
            {"status": "ok", "drives": None} if path is None else {"status": "missing"}
        )

    import httpx

    class FakeClient(httpx.AsyncClient):
        def __init__(self: Any, **k: Any) -> None:
            k["transport"] = httpx.MockTransport(lambda r: httpx.Response(404, json={}))
            super().__init__(**k)

    hw = {"rows": [], "alarms": [], "alarm_items": [], "checked": [], "notes": []}
    with (
        patch.object(server.httpx, "AsyncClient", FakeClient),
        patch.object(server, "get_gpu_stats", return_value=[]),
        patch.object(server, "_get_ip_info", return_value={}),
        patch.object(server, "get_hw_sensors", return_value=hw),
        patch.object(server, "_get_storage_health", health),
    ):
        st = server.APP.state
        st.ping_enabled = False
        st.history_processes_enabled = False
        st.history_process_count = 10
        st.history_sample_duration = 0
        st.history_sample_count = 1
        record = asyncio.run(
            server.collect_history_sample("http://glances.invalid", live=True)
        )
    assert "timestamp" in record


# ---- 7. the collector's whole-machine regex ---------------------------------------
def _bmc_total() -> re.Pattern[str]:
    text = (ROOT / "collector" / "health-sensors.sh").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("BMC_TOTAL = "))
    scope: dict[str, Any] = {"re": re}
    exec(line, scope)
    return scope["BMC_TOTAL"]


@pytest.mark.parametrize(
    "name",
    [
        "Pwr Consumption",
        "System Power",
        "Total Power",
        "Power Meter",
        "Total_Power",
        "Sys_Power",
        "SYS Power",
    ],
)
def test_07_bmc_total_names(name: str) -> None:
    assert _bmc_total().search(name), name


@pytest.mark.parametrize(
    "name", ["PS1 Input Power", "PSU2 Input Power", "CPU Power", "PS1 Output Power"]
)
def test_07b_not_totals(name: str) -> None:
    assert not _bmc_total().search(name), name


def _psu(name: str) -> bool:
    text = (ROOT / "collector" / "health-sensors.sh").read_text()
    line = next((ln for ln in text.splitlines() if ln.startswith("PSU_INPUT = ")), None)
    if line is None:
        return False  # HEAD: the collector does not classify PSU inputs
    scope: dict[str, Any] = {"re": re}
    exec(line, scope)
    return bool(scope["PSU_INPUT"].search(name))


def test_07c_per_psu_input_is_not_a_part() -> None:
    import server

    snap = {
        "power": [
            {
                "label": "PS1 Input Power",
                "value": 120,
                "unit": "W",
                "total": False,
                "psu_input": _psu("PS1 Input Power"),
            },
            {
                "label": "PS2 Input Power",
                "value": 118,
                "unit": "W",
                "total": False,
                "psu_input": _psu("PS2 Input Power"),
            },
            {"label": "CPU 0", "value": 80, "unit": "W", "total": False},
        ]
    }
    # What the collector would write for these names today (_bmc_total false).
    rows = server.collector_power_rows(snap)
    parts = [r["label"] for r in rows if not r["total"]]
    assert "PS1 Input Power" not in parts, "PSU wall input summed with CPU 0 as a part"
    assert [r for r in rows if r["total"]] == [
        {"label": "PSU input", "value": 238, "total": True}
    ]


# ---- 8. amdgpu APU ---------------------------------------------------------------
def test_08_amdgpu_apu_ppt_not_double_counted_as_gpu(tmp_path: Path) -> None:
    import hwmon

    files = {
        "hwmon/hwmon0/name": "k10temp",
        "hwmon/hwmon0/temp1_input": "50000",
        "hwmon/hwmon1/name": "amdgpu",
        "hwmon/hwmon1/power1_average": "15000000",
        "hwmon/hwmon1/power1_label": "slowPPT",
        "hwmon/hwmon1/power2_average": "18000000",
        "hwmon/hwmon1/power2_label": "fastPPT",
    }
    rows = hwmon.read_sensors(tree(tmp_path, files) / "hwmon", tmp_path / "thermal")
    power = by_type(rows, hwmon.POWER)
    assert len(power) == 1, f"slowPPT and fastPPT both summed as parts: {power}"
    assert not next(iter(power)).startswith("GPU"), "package power labelled GPU"


# ---- 10. duplicate chips: names follow probe order --------------------------------
def test_10_duplicate_chip_names_follow_the_device_not_probe_order(
    tmp_path: Path,
) -> None:
    import hwmon

    def layout(root: Path, first: str, second: str) -> dict[str, float]:
        files = {
            "devices/a/x": "",
            "devices/b/x": "",
            "hwmon/hwmon1/name": "thinkpad",
            "hwmon/hwmon1/fan1_label": "CPU Fan",
            "hwmon/hwmon2/name": "thinkpad",
            "hwmon/hwmon2/fan1_label": "CPU Fan",
            "hwmon/hwmon1/fan1_input": "1000" if first == "a" else "2000",
            "hwmon/hwmon2/fan1_input": "1000" if second == "a" else "2000",
        }
        tree(
            root,
            files,
            {
                "hwmon/hwmon1/device": f"../../devices/{first}",
                "hwmon/hwmon2/device": f"../../devices/{second}",
            },
        )
        rows = hwmon.read_sensors(root / "hwmon", root / "thermal")
        return {r["label"]: r["value"] for r in rows if r["type"] == hwmon.FAN}

    boot1 = layout(tmp_path / "b1", "a", "b")
    boot2 = layout(
        tmp_path / "b2", "b", "a"
    )  # the same two devices probed the other way
    assert boot1 == boot2, f"device a is {boot1} on one boot, {boot2} on the next"


# ---- 11. temperature order vs Glances --------------------------------------------
def test_11_temperature_order_is_glances_natural_order(tmp_path: Path) -> None:
    import hwmon

    def natural_keys(text: str) -> list[Any]:  # glances.globals.natural_keys
        return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", text)]

    files = {
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
    labels = [r["label"] for r in hwmon.read_sensors(tree(tmp_path, files) / "hwmon")]
    assert labels == sorted(labels, key=natural_keys), labels


# ---- 12. the BODEL fixture against the live box -----------------------------------
def test_12_bodel_fixture_matches_live_box(tmp_path: Path) -> None:
    import hwmon

    if not Path("/sys/class/hwmon/hwmon0").exists():
        pytest.skip("no live hwmon")
    live = sorted(r["label"] for r in hwmon.read_sensors() if r["type"] == hwmon.TEMP)
    fixture = sorted(
        r["label"]
        for r in hwmon.read_sensors(tree(tmp_path, BODEL) / "hwmon")
        if r["type"] == hwmon.TEMP
    )
    assert fixture == live


# ---- review follow-ups: alias text, keys, typos, reserved names --------------------
def test_alias_names_the_alarm_and_the_key_stays_raw() -> None:
    import hwmon

    _, alarms, _ = hwmon.fan_findings(
        [fan("f71889ed fan1", 0)],
        {"CPU fan": {"min_rpm": 300}},
        aliases={"f71889ed fan1": "CPU fan"},
    )
    assert alarms == [
        {
            "key": "fan/f71889ed fan1",
            "text": "fan CPU fan: 0 RPM, below its 300 RPM minimum",
        }
    ]


def test_a_ruled_fan_that_stops_answering_keeps_its_key() -> None:
    import hwmon

    _, alarms, _ = hwmon.fan_findings(
        [], {"CPU fan": {"min_rpm": 300}}, aliases={"f71889ed fan1": "CPU fan"}
    )
    assert [a["key"] for a in alarms] == ["fan/f71889ed fan1"]


def test_min_rpm_that_is_not_a_number_is_a_note() -> None:
    import hwmon

    _, alarms, notes = hwmon.fan_findings([fan("cpu", 0)], {"cpu": {"min_rpm": "300"}})
    assert alarms == []
    assert notes == ["fans config 'cpu': min_rpm '300' is not a number"]


def test_an_unreadable_fan_keeps_its_name(tmp_path: Path) -> None:
    import hwmon

    files: dict[str, str] = {
        "hwmon/hwmon0/name": "amdgpu",
        "hwmon/hwmon1/name": "amdgpu",
        "hwmon/hwmon1/fan1_input": "1500",
        "hwmon/hwmon0/fan1_input": "",  # present but unreadable
    }
    rows = hwmon.read_sensors(tree(tmp_path, files) / "hwmon")
    # The second card's fan stays "amdgpu fan1 2" while the first cannot be read.
    assert [r["label"] for r in rows if r["type"] == hwmon.FAN] == ["amdgpu fan1 2"]


def test_sensor_hide_leaves_sensors_out_by_name_or_alias(monkeypatch: Any) -> None:
    import re

    import server

    monkeypatch.setattr(
        server.hwmon,
        "read_sensors",
        lambda: [fan("unknown fan1", 900), fan("f71889ed fan1", 1200), fan("f71889ed fan2", 800)],
    )
    monkeypatch.setattr(server.hwmon, "setup_notes", lambda rows: [])
    monkeypatch.setattr(server, "TEMP_LABELS", {"f71889ed fan2": "Case fan 2"})
    monkeypatch.setattr(server, "_SENSOR_HIDE", [re.compile("unknown"), re.compile("Case fan")])
    monkeypatch.setitem(server._CONFIG, "fans", {})
    rows = server.get_hw_sensors()["rows"]
    assert [r["label"] for r in rows] == ["f71889ed fan1"]


def test_sensor_hide_skips_patterns_that_do_not_compile(monkeypatch: Any) -> None:
    import server

    monkeypatch.setitem(server._CONFIG, "sensor_hide", ["(", "nouveau", 3])
    assert [p.pattern for p in server._sensor_hide_patterns()] == ["nouveau"]
