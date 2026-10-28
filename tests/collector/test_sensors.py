#!/usr/bin/env python3
"""Exercise the real GPU shell capture and sensor parser without hardware/root."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT

SOURCE: str = (ROOT / "collector/health-sensors.sh").read_text()
SHELL: str
PYTHON: str
SHELL, PYTHON = SOURCE.split('python3 - "$tmp" "$OUT" <<\'PY\'\n', 1)
GPU_CAPTURE: str = SHELL[SHELL.index("if command -v nvidia-smi") :]
PYTHON = PYTHON.rsplit("\nPY", 1)[0]

FAKE_SMI: str = """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_SMI_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
kind = ('temperature' if '-q' in args else
        'extended' if 'clocks_event_reasons_counters.' in args[0] else 'basic')
reply = json.loads(Path(os.environ['FAKE_SMI_REPLIES']).read_text())[kind]
sys.stdout.write(reply.get('stdout', ''))
sys.stderr.write(reply.get('stderr', ''))
sys.exit(reply.get('status', 0))
"""


def gpu_csv(
    index: int = 0,
    bus: str = "00000000:01:00.0",
    core: int | str = 60,
    memory: int | str = 65,
    sw: str = "Not Active",
    ecc: int | str = 0,
    retired: str = "No",
    counters: bool = True,
) -> str:
    fields: list[str] = [
        str(index),
        bus,
        "NVIDIA RTX A6000",
        str(core),
        str(memory),
        sw,
        "Not Active",
        "Not Active",
        "1500000",
        "2000000",
        str(ecc),
        retired,
        "120.5",
        "300",
    ]
    if not counters:
        del fields[8:10]
    return ", ".join(fields) + "\n"


def tlimit_text(
    current: str = "2",
    threshold: str = "0",
    *,
    old: bool = False,
    bus: str = "00000000:01:00.0",
) -> str:
    current_key: str = "GPU T.Limit Temp" if old else "GPU Current T.Limit Temp"
    threshold_key: str = "GPU Max Operating T.Limit Temp" + (
        "" if old else " Specification"
    )
    return f"GPU {bus}\n    {current_key} : {current} C\n    {threshold_key} : {threshold} C\n"


class TestGPUSensors:
    def collect(
        self: TestGPUSensors,
        *,
        csv: str | None = None,
        temperature: str = "",
        extended: dict[str, str | int] | None = None,
        basic: dict[str, str | int] | None = None,
        config: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[list[str]]]:
        folder: str
        with tempfile.TemporaryDirectory(prefix="horatio-sensors-test-") as folder:
            stage: Path = Path(folder)
            name: str
            for name in ("fan", "temp", "power", "rapl1", "rapl2"):
                (stage / f"{name}.txt").write_text("")
            (stage / "config.json").write_text(json.dumps(config or {}))
            replies: dict[str, Any] = {
                "extended": extended
                if extended is not None
                else {"stdout": csv or gpu_csv()},
                "basic": basic
                if basic is not None
                else {"stdout": gpu_csv(counters=False)},
                "temperature": {"stdout": temperature},
            }
            (stage / "replies.json").write_text(json.dumps(replies))
            fake_bin: Path = stage / "bin"
            fake_bin.mkdir()
            command: Path = fake_bin / "nvidia-smi"
            command.write_text(f"#!{sys.executable}\n{FAKE_SMI}")
            command.chmod(0o755)
            env: dict[str, str] = dict(
                os.environ,
                PATH=f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                HEALTH_CONFIG=str(stage / "config.json"),
                FAKE_SMI_REPLIES=str(stage / "replies.json"),
                FAKE_SMI_CALLS=str(stage / "calls.jsonl"),
            )
            subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -uo pipefail\ntmp="$1"\n' + GPU_CAPTURE,
                    "bash",
                    str(stage),
                ],
                env=env,
                check=True,
                capture_output=True,
                timeout=10,
            )
            out: Path = stage / "sensors.json"
            subprocess.run(
                [sys.executable, "-", str(stage), str(out)],
                input=PYTHON,
                text=True,
                env=env,
                check=True,
                capture_output=True,
                timeout=10,
            )
            state: dict[str, Any] = json.loads(out.read_text())
            calls: list[list[str]] = [
                json.loads(line)
                for line in (stage / "calls.jsonl").read_text().splitlines()
            ]
            return state, calls

    def test_modern_driver_keeps_counters_and_all_columns(self: TestGPUSensors) -> None:
        state: dict[str, Any]
        calls: list[list[str]]
        state, calls = self.collect()
        gpu: dict[str, Any]
        (gpu,) = state["gpus"]
        assert (gpu["core_c"], gpu["mem_c"], gpu["power_w"], gpu["power_limit_w"]) == (
            60,
            65,
            120.5,
            300,
        )
        assert (gpu["sw_thermal_s"], gpu["hw_thermal_s"]) == (1.5, 2)
        assert state["alarms"] == []
        assert len(calls) == 2

    def test_rejected_optional_counter_retries_from_either_stream(
        self: TestGPUSensors,
        subtests: pytest.Subtests,
    ) -> None:
        reason: str
        for reason in ("sw", "hw"):
            stream: str
            for stream in ("stdout", "stderr"):
                with subtests.test(reason=reason, stream=stream):
                    diagnostic: str = (
                        f'Field "clocks_event_reasons_counters.{reason}_thermal_slowdown" '
                        "is not a valid field to query.\n"
                    )
                    basic: str = gpu_csv(
                        sw="Active", ecc=3, retired="Yes", counters=False
                    )
                    state: dict[str, Any]
                    calls: list[list[str]]
                    state, calls = self.collect(
                        extended={stream: diagnostic, "status": 2},
                        basic={"stdout": basic},
                    )
                    gpu: dict[str, Any]
                    (gpu,) = state["gpus"]
                    assert (gpu["sw_thermal_s"], gpu["hw_thermal_s"]) == (None, None)
                    assert (
                        gpu["mem_c"],
                        gpu["ecc_uncorrected"],
                        gpu["retired_pending"],
                        gpu["power_w"],
                        gpu["power_limit_w"],
                    ) == (65, 3, True, 120.5, 300)
                    assert state["alarms"] == [
                        "GPU 0: thermal slowdown active (sw)",
                        "GPU 0: 3 uncorrected ECC errors since driver load",
                        "GPU 0: retired pages pending",
                    ]
                    assert len(calls) == 3
                    assert "temperature.memory" in calls[1][0]
                    assert "clocks_event_reasons_counters" not in calls[1][0]

    def test_other_failures_do_not_retry_and_remain_alarms(
        self: TestGPUSensors,
        subtests: pytest.Subtests,
    ) -> None:
        status: int
        diagnostic: str
        for status, diagnostic in (
            (9, "Driver not loaded"),
            (124, ""),
            (2, 'Field "temperature.memory" is not a valid field to query.'),
        ):
            with subtests.test(status=status, diagnostic=diagnostic):
                state: dict[str, Any]
                calls: list[list[str]]
                state, calls = self.collect(
                    extended={"stderr": diagnostic, "status": status}
                )
                assert len(calls) == 2
                assert state["gpus"] == []
                assert any((f"exit {status}" in alarm for alarm in state["alarms"]))

    def test_failed_fallback_is_not_hidden_or_retried_again(
        self: TestGPUSensors,
    ) -> None:
        rejected: str = 'Field "clocks_event_reasons_counters.sw_thermal_slowdown" is not a valid field to query.'
        state: dict[str, Any]
        calls: list[list[str]]
        state, calls = self.collect(
            extended={"stdout": rejected, "status": 2},
            basic={"stderr": "Driver not loaded", "status": 9},
        )
        assert len(calls) == 3
        assert state["gpus"] == []
        assert state["alarms"] == ["collector: gpu: Driver not loadedexit 9"]

    def test_truncated_modern_row_is_not_mistaken_for_fallback(
        self: TestGPUSensors,
    ) -> None:
        state: dict[str, Any]
        state, _ = self.collect(csv=gpu_csv(counters=False))
        assert state["gpus"] == []
        assert state["alarms"][0].startswith("collector: gpu: unparsed line:")

    def test_absolute_core_and_memory_limits_keep_existing_rules(
        self: TestGPUSensors,
    ) -> None:
        temperature: str = """GPU 00000000:01:00.0
    GPU Shutdown Temp : 100 C
    GPU Slowdown Temp : 90 C
    GPU Max Operating Temp : 85 C
    Memory Max Operating Temp : 87 C
"""
        state: dict[str, Any]
        state, _ = self.collect(
            csv=gpu_csv(core=84, memory=86), temperature=temperature
        )
        assert state["alarms"] == [
            "GPU 0: memory 86 C >= max operating 87 C - 3",
            "GPU 0: core 84 C >= max operating 85 C - 3",
        ]
        limits: dict[str, float | None] = state["gpus"][0]["limits"]
        assert [
            limits[key] for key in ("shutdown", "slowdown", "max_op", "mem_max_op")
        ] == [100, 90, 85, 87]

    def test_old_and_new_tlimit_labels_use_relative_threshold(
        self: TestGPUSensors,
        subtests: pytest.Subtests,
    ) -> None:
        old: bool
        for old in (False, True):
            current: str
            threshold: str
            for current, threshold in (("2", "0"), ("-3", "-5"), ("3", "0")):
                with subtests.test(old=old, current=current, threshold=threshold):
                    state: dict[str, Any]
                    state, _ = self.collect(
                        temperature=tlimit_text(current, threshold, old=old)
                    )
                    assert state["alarms"] == [
                        f"GPU 0: T.Limit {current} C <= max operating {threshold} C + 3"
                    ]
                    limits: dict[str, float | None] = state["gpus"][0]["limits"]
                    assert limits["max_op"] is None
                    assert limits["mem_max_op"] is None
                    assert limits["current_tlimit"] == float(current)

    def test_far_or_unavailable_tlimit_has_no_temperature_alarm(
        self: TestGPUSensors,
        subtests: pytest.Subtests,
    ) -> None:
        current: str
        threshold: str
        for current, threshold in (
            ("4", "0"),
            ("-1", "-5"),
            ("N/A", "0"),
            ("1", "N/A"),
        ):
            with subtests.test(current=current, threshold=threshold):
                state: dict[str, Any]
                state, _ = self.collect(temperature=tlimit_text(current, threshold))
                assert state["alarms"] == []

    def test_tlimit_obeys_custom_and_disabled_margin(self: TestGPUSensors) -> None:
        state: dict[str, Any]
        state, _ = self.collect(
            temperature=tlimit_text("4"), config={"gpu_temp_alarm_margin_c": 5}
        )
        assert state["alarms"] == ["GPU 0: T.Limit 4 C <= max operating 0 C + 5"]
        state, _ = self.collect(
            csv=gpu_csv(sw="Active"),
            temperature=tlimit_text("-1"),
            config={"gpu_temp_alarm_margin_c": None},
        )
        assert state["alarms"] == ["GPU 0: thermal slowdown active (sw)"]

    def test_limits_match_bus_id_not_order_and_ignore_case(
        self: TestGPUSensors,
    ) -> None:
        csv: str = gpu_csv(bus="00000000:0a:00.0") + gpu_csv(
            index=1, bus="00000000:0b:00.0"
        )
        temperature: str = tlimit_text("9", bus="00000000:0B:00.0") + tlimit_text(
            "1", bus="00000000:0A:00.0"
        )
        state: dict[str, Any]
        state, _ = self.collect(csv=csv, temperature=temperature)
        assert state["alarms"] == ["GPU 0: T.Limit 1 C <= max operating 0 C + 3"]
        assert [gpu["limits"]["current_tlimit"] for gpu in state["gpus"]] == [1, 9]
