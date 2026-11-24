"""Unknown readings cannot clear an alarm: real collectors through the tracker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.collector.test_collect import collect, covered, smart
from tests.collector.test_sensors import TestGPUSensors as SensorCapture
from tests.collector.test_sensors import gpu_csv


@pytest.fixture
def tracker(app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in ("hwmon", "hostcheck", "zfs", "server"):
        spec = importlib.util.spec_from_file_location(
            name, app_dir / "python" / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


def transition(
    tracker: ModuleType,
    fault: dict[str, Any],
    unknown: dict[str, Any],
    healthy: dict[str, Any],
    key: str,
    clear_after: int = 3,
) -> None:
    known, _ = tracker.track_alarms(
        {}, fault["alarm_items"], fault["checked"], 1000, clear_after
    )
    assert key in known
    for now in (1060, 1120, 1180):
        known, events = tracker.track_alarms(
            known, unknown["alarm_items"], unknown["checked"], now, clear_after
        )
        assert not any(e["event"] == "cleared" and e["key"] == key for e in events)
        assert known[key]["since"] == 1000
    known, _ = tracker.track_alarms(
        known, fault["alarm_items"], fault["checked"], 1240, clear_after
    )
    assert known[key]["since"] == 1000
    for now in range(1300, 1300 + 60 * clear_after, 60):
        known, events = tracker.track_alarms(
            known, healthy["alarm_items"], healthy["checked"], now, clear_after
        )
    assert key not in known
    assert any(e["event"] == "cleared" and e["key"] == key for e in events)


def test_bmc_unreadable_sensor_and_missing_fan_hold(tracker: ModuleType) -> None:
    runner = SensorCapture()

    def capture(fans: str) -> dict[str, Any]:
        return runner.collect(
            files={"fan.txt": fans}, config={"fan_rpm_alarm_above": 1500}
        )[0]

    fault = capture(
        "Fan1 | 30h | cr | 7.1 | 2000 RPM\nFan2 | 31h | ok | 7.2 | 1000 RPM\n"
    )
    unknown = capture(
        "Fan1 | 30h | ns | 7.1 | No Reading\nFan2 | 31h | ok | 7.2 | 1000 RPM\n"
    )
    missing = capture("Fan2 | 31h | ok | 7.2 | 1000 RPM\n")
    healthy = capture(
        "Fan1 | 30h | ok | 7.1 | 1000 RPM\nFan2 | 31h | ok | 7.2 | 1000 RPM\n"
    )
    for key in ("bmc/fan/Fan1@7.1", "fans/ceiling/Fan1@7.1"):
        transition(tracker, fault, unknown, healthy, key)
        transition(tracker, fault, missing, healthy, key)
    disabled = runner.collect(config={"fan_rpm_alarm_above": None})[0]
    assert covered(disabled, "fans/ceiling/Fan1@7.1")


@pytest.mark.parametrize(
    "field,key", [("ecc", "ecc"), ("retired", "retired_pages"), ("sw", "slowdown")]
)
def test_gpu_unavailable_field_holds_independently(
    tracker: ModuleType, field: str, key: str
) -> None:
    runner = SensorCapture()
    fault = runner.collect(csv=gpu_csv(ecc=5, retired="Yes", sw="Active"))[0]
    fields: dict[str, Any] = {
        "ecc": 0,
        "retired": "No",
        "sw": "Not Active",
        field: "[N/A]",
    }
    unknown = runner.collect(csv=gpu_csv(**fields))[0]
    healthy = runner.collect(csv=gpu_csv())[0]
    transition(tracker, fault, unknown, healthy, f"gpu/pci/0000:01:00.0/{key}")
    other = (
        "gpu/pci/0000:01:00.0/retired_pages"
        if field == "ecc"
        else "gpu/pci/0000:01:00.0/ecc"
    )
    assert covered(unknown, other)


def test_gpu_missing_limits_hold_but_disabled_rule_clears(tracker: ModuleType) -> None:
    runner = SensorCapture()
    limit = "GPU 00000000:01:00.0\n    GPU Max Operating Temp : 70 C\n"
    fault = runner.collect(csv=gpu_csv(core=69), temperature=limit)[0]
    unknown = runner.collect(csv=gpu_csv(core=60), temperature="")[0]
    healthy = runner.collect(csv=gpu_csv(core=60), temperature=limit)[0]
    transition(tracker, fault, unknown, healthy, "gpu/pci/0000:01:00.0/temp/core")
    assert covered(
        runner.collect(config={"gpu_temp_alarm_margin_c": None})[0],
        "gpu/pci/0000:01:00.0/temp/core",
    )


def test_missing_dimm_does_not_clear_its_uncorrectable_error(
    tracker: ModuleType, tmp_path: Path
) -> None:
    second: dict[str, str | None] = {
        "edac/mc0/dimm1/dimm_label": "DIMM#1",
        "edac/mc0/dimm1/dimm_ce_count": "0",
        "edac/mc0/dimm1/dimm_ue_count": "0",
    }
    fault = collect(
        tmp_path / "fault", {**second, "edac/mc0/dimm0/dimm_ue_count": "1"}
    )[0]
    unknown = collect(
        tmp_path / "unknown", {**second, "edac/mc0/dimm0/dimm_label": None}
    )[0]
    healthy = collect(tmp_path / "healthy", second)[0]
    transition(tracker, fault, unknown, healthy, "ecc/dimm/mc0/dimm0/uncorrectable")
    assert not covered(
        unknown, "ecc/uncorrectable"
    )  # old aggregate evidence is not reassigned


def test_corrected_ecc_retains_sum_and_inventory_dependencies(
    tracker: ModuleType, tmp_path: Path
) -> None:
    state = collect(
        tmp_path,
        {
            "edac/mc0/dimm0/dimm_ce_count": "30",
            "edac/mc0/dimm1/dimm_label": "DIMM#1",
            "edac/mc0/dimm1/dimm_ce_count": "30",
            "edac/mc0/dimm1/dimm_ue_count": "0",
        },
    )[0]
    alarm = next(a for a in state["alarm_items"] if a["key"] == "ecc/correctable")
    assert alarm["value"] == 60
    assert alarm["requires"] == [
        "ecc/dimm/mc0/dimm0/correctable",
        "ecc/dimm/mc0/dimm1/correctable",
    ]
    assert all(covered(state, key) for key in alarm["requires"])
    assert not covered(state, "ecc/correctable")
    unknown = collect(
        tmp_path / "unknown",
        {
            "edac/mc0/dimm0/dimm_label": None,
            "edac/mc0/dimm1/dimm_label": "DIMM#1",
            "edac/mc0/dimm1/dimm_ce_count": "30",
            "edac/mc0/dimm1/dimm_ue_count": "0",
        },
    )[0]
    healthy = collect(
        tmp_path / "healthy",
        {
            "edac/mc0/dimm1/dimm_label": "DIMM#1",
            "edac/mc0/dimm1/dimm_ce_count": "0",
            "edac/mc0/dimm1/dimm_ue_count": "0",
        },
    )[0]
    transition(tracker, state, unknown, healthy, "ecc/correctable")


@pytest.mark.parametrize("fault_rc", [128, 128 | 4])
def test_unreadable_selftest_log_holds_previous_failure(
    tracker: ModuleType, tmp_path: Path, fault_rc: int
) -> None:
    report: dict[str, Any] = {
        "serial_number": "S1",
        "smart_status": {"passed": True},
        "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}]},
    }

    def capture(name: str, rc: int, errors: int | None) -> dict[str, Any]:
        stage = tmp_path / name
        data = dict(report)
        if errors is not None:
            data["ata_smart_self_test_log"] = {
                "standard": {"count": 1, "error_count_total": errors}
            }
        return collect(stage, {**smart(stage, "sda", rc, data)})[0]

    fault = capture("fault", fault_rc, 1)
    unknown = capture("unknown", 4, None)
    healthy = capture("healthy", 0, 0)
    assert covered(fault, "smart/S1/selftest")
    assert not covered(unknown, "smart/S1/selftest")
    assert covered(unknown, "smart/S1/health")
    assert covered(unknown, "smart/S1/reallocated")
    transition(tracker, fault, unknown, healthy, "smart/S1/selftest", clear_after=1)


def test_missing_scalar_fields_are_unknown_not_zero(tmp_path: Path) -> None:
    report = {
        "serial_number": "S1",
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {"percentage_used": 0},
        "ata_smart_attributes": {"table": [{"id": 5, "raw": {}}]},
    }
    state = collect(
        tmp_path,
        {
            **smart(tmp_path, "sda", 0, report),
            "edac/mc0/dimm0/dimm_ue_count": None,
            "block/md0/md/dev-sda1/state": "in_sync",
            "block/md0/md/sync_action": "idle",
        },
    )[0]
    for key in (
        "smart/S1/media_errors",
        "smart/S1/reallocated",
        "smart/S1/temperature",
        "ecc/dimm/mc0/dimm0/uncorrectable",
        "ecc/dimm/mc0/dimm0/correctable",
        "md/md0/degraded",
        "md/md0/mismatch",
        "md/md0/member/sda1/errors",
    ):
        assert not covered(state, key), key
    assert covered(state, "smart/S1/percentage_used")
    assert covered(state, "md/md0/member/sda1/state")
    assert state["edac"]["ue_total"] is None


def test_missing_psu_reading_keeps_role_without_confusing_amps() -> None:
    state = SensorCapture().collect(
        files={
            "power.txt": "PS1 Input Power | 70h | ok | 10.1 | 100 Watts\nPS2 Input Power | 71h | ns | 10.2 | No Reading\nPS3 Input Current | 72h | ns | 10.3 | No Reading\n"
        }
    )[0]
    assert [(r["value"], r["psu_input"]) for r in state["power"]] == [
        (100.0, True),
        (None, True),
        (None, False),
    ]


def test_gpu_identity_survives_index_changes_and_holds_legacy_evidence(
    tracker: ModuleType,
) -> None:
    runner = SensorCapture()
    fault = runner.collect(
        csv=gpu_csv(index=0, bus="00000000:01:00.0", ecc=5)
        + gpu_csv(index=1, bus="00000000:02:00.0")
    )[0]
    survivor = runner.collect(csv=gpu_csv(index=0, bus="00000000:02:00.0"))[0]
    physical = "gpu/pci/0000:01:00.0/ecc"
    legacy = "gpu/0/ecc"
    known, _ = tracker.track_alarms(
        {},
        fault["alarm_items"] + [{"key": legacy, "text": "legacy ECC fault"}],
        fault["checked"],
        1000,
        3,
    )
    for now in (1060, 1120, 1180):
        known, events = tracker.track_alarms(
            known, survivor["alarm_items"], survivor["checked"], now, 3
        )
        assert events == []
        assert known[physical]["since"] == known[legacy]["since"] == 1000
        assert (
            "unchecked_since" in known[physical] and "unchecked_since" in known[legacy]
        )
    returned = runner.collect(csv=gpu_csv(index=1, bus="0000:01:00.0", ecc=5))[0]
    known, events = tracker.track_alarms(
        known, returned["alarm_items"], returned["checked"], 1240, 3
    )
    assert events == [] and known[physical]["since"] == 1000
    healthy = runner.collect(csv=gpu_csv(index=1, bus="00000000:01:00.0"))[0]
    for now in (1300, 1360, 1420):
        known, events = tracker.track_alarms(
            known, healthy["alarm_items"], healthy["checked"], now, 3
        )
    assert physical not in known and legacy in known
    assert [e["key"] for e in events if e["event"] == "cleared"] == [physical]


def test_unreadable_gpu_bus_identity_does_not_check_index_alarms() -> None:
    state = SensorCapture().collect(csv=gpu_csv(bus="[N/A]", ecc=5))[0]
    assert state["gpus"] == []
    assert any("invalid PCI bus identity" in text for text in state["alarms"])
    assert not any(prefix.startswith("gpu/") for prefix in state["checked"])
