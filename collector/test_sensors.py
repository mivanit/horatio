#!/usr/bin/env python3
"""Exercise the real GPU shell capture and sensor parser without hardware/root."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).with_name("health-sensors.sh").read_text()
SHELL, PYTHON = SOURCE.split('python3 - "$tmp" "$OUT" <<\'PY\'\n', 1)
GPU_CAPTURE = SHELL[SHELL.index("if command -v nvidia-smi"):]
PYTHON = PYTHON.rsplit("\nPY", 1)[0]

FAKE_SMI = """import json, os, sys
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


def gpu_csv(index=0, bus="00000000:01:00.0", core=60, memory=65,
            sw="Not Active", ecc=0, retired="No", counters=True):
    fields = [str(index), bus, "NVIDIA RTX A6000", str(core), str(memory),
              sw, "Not Active", "Not Active", "1500000", "2000000",
              str(ecc), retired, "120.5", "300"]
    if not counters:
        del fields[8:10]
    return ", ".join(fields) + "\n"


def tlimit_text(current="2", threshold="0", *, old=False, bus="00000000:01:00.0"):
    current_key = "GPU T.Limit Temp" if old else "GPU Current T.Limit Temp"
    threshold_key = "GPU Max Operating T.Limit Temp" + ("" if old else " Specification")
    return f"GPU {bus}\n    {current_key} : {current} C\n    {threshold_key} : {threshold} C\n"


class GPUSensorTests(unittest.TestCase):
    def collect(self, *, csv=None, temperature="", extended=None, basic=None, config=None):
        with tempfile.TemporaryDirectory(prefix="horatio-sensors-test-") as folder:
            stage = Path(folder)
            for name in ("fan", "temp", "power", "rapl1", "rapl2"):
                (stage / f"{name}.txt").write_text("")
            (stage / "config.json").write_text(json.dumps(config or {}))
            replies = {
                "extended": extended if extended is not None else {"stdout": csv or gpu_csv()},
                "basic": basic if basic is not None else {"stdout": gpu_csv(counters=False)},
                "temperature": {"stdout": temperature},
            }
            (stage / "replies.json").write_text(json.dumps(replies))
            fake_bin = stage / "bin"
            fake_bin.mkdir()
            command = fake_bin / "nvidia-smi"
            command.write_text(f"#!{sys.executable}\n{FAKE_SMI}")
            command.chmod(0o755)
            env = dict(os.environ, PATH=f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                       HEALTH_CONFIG=str(stage / "config.json"),
                       FAKE_SMI_REPLIES=str(stage / "replies.json"),
                       FAKE_SMI_CALLS=str(stage / "calls.jsonl"))
            subprocess.run(["bash", "-c", 'set -uo pipefail\ntmp="$1"\n' + GPU_CAPTURE,
                            "bash", str(stage)], env=env, check=True, capture_output=True, timeout=10)
            out = stage / "sensors.json"
            subprocess.run([sys.executable, "-", str(stage), str(out)], input=PYTHON,
                           text=True, env=env, check=True, capture_output=True, timeout=10)
            state = json.loads(out.read_text())
            calls = [json.loads(line) for line in (stage / "calls.jsonl").read_text().splitlines()]
            return state, calls

    def test_modern_driver_keeps_counters_and_all_columns(self):
        state, calls = self.collect()
        gpu, = state["gpus"]
        self.assertEqual((gpu["core_c"], gpu["mem_c"], gpu["power_w"], gpu["power_limit_w"]),
                         (60, 65, 120.5, 300))
        self.assertEqual((gpu["sw_thermal_s"], gpu["hw_thermal_s"]), (1.5, 2))
        self.assertEqual(state["alarms"], [])
        self.assertEqual(len(calls), 2)

    def test_rejected_optional_counter_retries_from_either_stream(self):
        for reason in ("sw", "hw"):
            for stream in ("stdout", "stderr"):
                with self.subTest(reason=reason, stream=stream):
                    diagnostic = (f'Field "clocks_event_reasons_counters.{reason}_thermal_slowdown" '
                                  'is not a valid field to query.\n')
                    basic = gpu_csv(sw="Active", ecc=3, retired="Yes", counters=False)
                    state, calls = self.collect(extended={stream: diagnostic, "status": 2},
                                                basic={"stdout": basic})
                    gpu, = state["gpus"]
                    self.assertEqual((gpu["sw_thermal_s"], gpu["hw_thermal_s"]), (None, None))
                    self.assertEqual((gpu["mem_c"], gpu["ecc_uncorrected"], gpu["retired_pending"],
                                      gpu["power_w"], gpu["power_limit_w"]), (65, 3, True, 120.5, 300))
                    self.assertEqual(state["alarms"], ["GPU 0: thermal slowdown active (sw)",
                                                      "GPU 0: 3 uncorrected ECC errors since driver load",
                                                      "GPU 0: retired pages pending"])
                    self.assertEqual(len(calls), 3)
                    self.assertIn("temperature.memory", calls[1][0])
                    self.assertNotIn("clocks_event_reasons_counters", calls[1][0])

    def test_other_failures_do_not_retry_and_remain_alarms(self):
        for status, diagnostic in ((9, "Driver not loaded"), (124, ""),
                                   (2, 'Field "temperature.memory" is not a valid field to query.')):
            with self.subTest(status=status, diagnostic=diagnostic):
                state, calls = self.collect(extended={"stderr": diagnostic, "status": status})
                self.assertEqual(len(calls), 2)
                self.assertEqual(state["gpus"], [])
                self.assertTrue(any(f"exit {status}" in alarm for alarm in state["alarms"]))

    def test_failed_fallback_is_not_hidden_or_retried_again(self):
        rejected = 'Field "clocks_event_reasons_counters.sw_thermal_slowdown" is not a valid field to query.'
        state, calls = self.collect(extended={"stdout": rejected, "status": 2},
                                    basic={"stderr": "Driver not loaded", "status": 9})
        self.assertEqual(len(calls), 3)
        self.assertEqual(state["gpus"], [])
        self.assertEqual(state["alarms"], ["collector: gpu: Driver not loadedexit 9"])

    def test_truncated_modern_row_is_not_mistaken_for_fallback(self):
        state, _ = self.collect(csv=gpu_csv(counters=False))
        self.assertEqual(state["gpus"], [])
        self.assertTrue(state["alarms"][0].startswith("collector: gpu: unparsed line:"))

    def test_absolute_core_and_memory_limits_keep_existing_rules(self):
        temperature = """GPU 00000000:01:00.0
    GPU Shutdown Temp : 100 C
    GPU Slowdown Temp : 90 C
    GPU Max Operating Temp : 85 C
    Memory Max Operating Temp : 87 C
"""
        state, _ = self.collect(csv=gpu_csv(core=84, memory=86), temperature=temperature)
        self.assertEqual(state["alarms"], ["GPU 0: memory 86 C >= max operating 87 C - 3",
                                          "GPU 0: core 84 C >= max operating 85 C - 3"])
        limits = state["gpus"][0]["limits"]
        self.assertEqual([limits[key] for key in ("shutdown", "slowdown", "max_op", "mem_max_op")],
                         [100, 90, 85, 87])

    def test_old_and_new_tlimit_labels_use_relative_threshold(self):
        for old in (False, True):
            for current, threshold in (("2", "0"), ("-3", "-5"), ("3", "0")):
                with self.subTest(old=old, current=current, threshold=threshold):
                    state, _ = self.collect(temperature=tlimit_text(current, threshold, old=old))
                    self.assertEqual(state["alarms"],
                                     [f"GPU 0: T.Limit {current} C <= max operating {threshold} C + 3"])
                    limits = state["gpus"][0]["limits"]
                    self.assertIsNone(limits["max_op"])
                    self.assertIsNone(limits["mem_max_op"])
                    self.assertEqual(limits["current_tlimit"], float(current))

    def test_far_or_unavailable_tlimit_has_no_temperature_alarm(self):
        for current, threshold in (("4", "0"), ("-1", "-5"), ("N/A", "0"), ("1", "N/A")):
            with self.subTest(current=current, threshold=threshold):
                state, _ = self.collect(temperature=tlimit_text(current, threshold))
                self.assertEqual(state["alarms"], [])

    def test_tlimit_obeys_custom_and_disabled_margin(self):
        state, _ = self.collect(temperature=tlimit_text("4"), config={"gpu_temp_alarm_margin_c": 5})
        self.assertEqual(state["alarms"], ["GPU 0: T.Limit 4 C <= max operating 0 C + 5"])
        state, _ = self.collect(csv=gpu_csv(sw="Active"), temperature=tlimit_text("-1"),
                                config={"gpu_temp_alarm_margin_c": None})
        self.assertEqual(state["alarms"], ["GPU 0: thermal slowdown active (sw)"])

    def test_limits_match_bus_id_not_order_and_ignore_case(self):
        csv = gpu_csv(bus="00000000:0a:00.0") + gpu_csv(index=1, bus="00000000:0b:00.0")
        temperature = (tlimit_text("9", bus="00000000:0B:00.0")
                       + tlimit_text("1", bus="00000000:0A:00.0"))
        state, _ = self.collect(csv=csv, temperature=temperature)
        self.assertEqual(state["alarms"], ["GPU 0: T.Limit 1 C <= max operating 0 C + 3"])
        self.assertEqual([gpu["limits"]["current_tlimit"] for gpu in state["gpus"]], [1, 9])


if __name__ == "__main__":
    unittest.main()
