from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest.mock
from pathlib import Path
from typing import Any
from unittest.mock import patch


def test_gpu_info_partial_unit() -> None:
    """Unsupported nvidia-smi fields must not hide usable GPU metadata."""

    import server

    output: str = (
        "0, NVIDIA H100 80GB HBM3, [N/A], 87, 81559, 30, 141, 700, 580.65.06\n"
        "1, NVIDIA Virtual GPU, 0, N/A, 8192, [Not Supported], nan, inf, 580.65.06\n"
        "bad, invalid index, 0, 0, 8192, 30, 0, 100, 580.65.06\n"
    )
    with patch.object(
        server.subprocess,
        "run",
        side_effect=[
            subprocess.CompletedProcess([], 0, output, ""),
            subprocess.CompletedProcess([], 0, "CUDA Version: 13.0\n", ""),
        ],
    ):
        gpus: list[dict[str, float | str | None]] = server._get_gpu_info()
    assert [g["index"] for g in gpus] == [0, 1], (
        "GPU info: unavailable metrics preserve rows; invalid index is skipped"
    )
    if len(gpus) != 2:
        return
    assert (
        gpus[0]["utilization"] is None
        and gpus[0]["memory_used"] == 87
        and (gpus[0]["memory_total"] == 81559)
        and (gpus[0]["temperature"] == 30)
    ), "GPU info: MIG utilization N/A preserves memory and temperature"
    assert (
        gpus[0]["name"] == "NVIDIA H100 80GB HBM3"
        and gpus[0]["driver_version"] == "580.65.06"
        and (gpus[0]["cuda_version"] == "13.0")
    ), "GPU info: partial readings preserve device identity and versions"
    assert (
        gpus[1]["utilization"] == 0
        and gpus[1]["memory_total"] == 8192
        and (gpus[1]["memory_used"] is None)
        and (gpus[1]["temperature"] is None)
    ), "GPU info: genuine zero and known capacity survive other unavailable fields"
    assert gpus[1]["power_draw"] is None and gpus[1]["power_limit"] is None, (
        "GPU info: non-finite metrics stay unavailable"
    )
    assert json.loads(json.dumps(gpus, allow_nan=False)) == gpus, (
        "GPU info: partial readings serialize as valid JSON"
    )


def test_gpu_stats_compatibility_unit() -> None:
    """An unsupported optional counter must not discard basic GPU readings."""

    import server

    basic: str = "0, 0, 4096, 24576, 53, 123, 86"
    modern: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        [], 0, basic + ", 2500000, 1000000\n", ""
    )
    run: unittest.mock.MagicMock | unittest.mock.AsyncMock
    with patch.object(server.subprocess, "run", return_value=modern) as run:
        gpus: list[dict[str, Any]] = server.get_gpu_stats()
    assert (
        run.call_count == 1
        and len(gpus) == 1
        and (gpus[0]["throttle_sw_thermal_us"] == 2500000)
        and (gpus[0]["throttle_hw_thermal_us"] == 1000000)
    ), "GPU stats: modern drivers retain counters with one query"

    # Different CLI versions can put the invalid-field diagnostic on either stream.
    counter: str
    stream: str
    for counter, stream in (("sw", "stderr"), ("hw", "stdout")):
        error: str = f'Field "clocks_event_reasons_counters.{counter}_thermal_slowdown" is not a valid field to query.\n'
        rejected: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            [],
            2,
            error if stream == "stdout" else "",
            error if stream == "stderr" else "",
        )
        fallback: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            [], 0, basic + "\n", ""
        )
        with patch.object(
            server.subprocess, "run", side_effect=[rejected, fallback]
        ) as run:
            gpus = server.get_gpu_stats()
        assert (
            run.call_count == 2
            and len(gpus) == 1
            and (gpus[0]["util_percent"] == 0)
            and (gpus[0]["memory_used_mb"] == 4096)
            and (gpus[0]["memory_total_mb"] == 24576)
            and (gpus[0]["temperature"] == 53)
            and (gpus[0]["temperature_memory"] == 86)
            and (gpus[0]["power_draw"] == 123)
            and (gpus[0]["throttle_sw_thermal_us"] is None)
            and (gpus[0]["throttle_hw_thermal_us"] is None)
            and ("clocks_event_reasons_counters" not in run.call_args.args[0][1])
            and ("temperature.memory" in run.call_args.args[0][1])
        ), (
            f"GPU stats: unsupported {counter} counter on {stream} preserves basic readings"
        )

    label: str
    failure: subprocess.CompletedProcess[str]
    for label, failure in (
        ("driver error", subprocess.CompletedProcess([], 9, "", "driver not loaded")),
        (
            "unrelated invalid field",
            subprocess.CompletedProcess(
                [], 2, "", 'Field "other" is not a valid field to query.'
            ),
        ),
    ):
        with patch.object(server.subprocess, "run", return_value=failure) as run:
            gpus = server.get_gpu_stats()
        assert gpus == [] and run.call_count == 1, f"GPU stats: {label} does not retry"
    with patch.object(
        server.subprocess, "run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)
    ) as run:
        gpus = server.get_gpu_stats()
    assert gpus == [] and run.call_count == 1, "GPU stats: timeout does not retry"

    failure = subprocess.CompletedProcess([], 9, "", "fallback driver failure")
    warning: unittest.mock.MagicMock | unittest.mock.AsyncMock
    with (
        patch.object(server.subprocess, "run", side_effect=[rejected, failure]) as run,
        patch.object(server.logger, "warning") as warning,
    ):
        gpus = server.get_gpu_stats()
    assert (
        gpus == []
        and run.call_count == 2
        and (warning.call_args.args[-1] == failure.stderr)
    ), "GPU stats: failed fallback is reported without further retries"


def test_gpu_processes_unit() -> None:
    """_get_gpu_processes against a fake nvidia-smi on PATH (real subprocess, so
    the decoding is exercised): the (gpu index, pid) join of compute-apps with
    pmon, a pmon command name that ends mid-character, and a stalled pmon that
    must cost only the utilization columns, quickly."""
    import server

    script: str = r"""#!/bin/sh
case "$*" in
  "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory --format=csv,noheader,nounits")
    printf 'GPU-aaa, 4242, /usr/bin/burn, 100\nGPU-bbb, 4242, /usr/bin/burn, 200\nGPU-aaa, 5151, /opt/caf\303\251, 50\n' ;;
  "--query-gpu=index,uuid --format=csv,noheader")
    printf '0, GPU-aaa\n1, GPU-bbb\n' ;;
  "pmon -c 1 -s u")
    [ -n "$FAKE_PMON_SLEEP" ] && sleep "$FAKE_PMON_SLEEP"
    printf '# gpu  pid  type  sm  mem  enc  dec  jpg  ofa  command\n'
    printf '    0  4242  C  70  40  -  -  -  -  burn\n'
    printf '    1  4242  C  20  10  -  -  -  -  burn\n'
    printf '    0  5151  C   -   -  -  -  -  -  caf\303\n' ;;
  *) exit 1 ;;
esac
"""
    orig_path: str = os.environ.get("PATH", "")
    tmp: str
    with tempfile.TemporaryDirectory() as tmp:
        fake: Path = Path(tmp) / "nvidia-smi"
        fake.write_text(script)
        fake.chmod(0o755)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{orig_path}"
        try:
            rows: list[dict[str, Any]] = server._get_gpu_processes()
            got: dict[tuple[Any, Any], tuple[Any, ...]] = {
                (r["gpu_index"], r["pid"]): (
                    r["gpu_memory_mib"],
                    r["gpu_util_percent"],
                    r["gpu_mem_util_percent"],
                )
                for r in rows
            }
            assert got == {
                (0, 4242): (100.0, 70.0, 40.0),
                (1, 4242): (200.0, 20.0, 10.0),
                (0, 5151): (50.0, None, None),
            }, (
                f"_get_gpu_processes: compute-apps joined with pmon on (gpu index, pid); '-' -> None; non-UTF-8 names don't raise: {repr(got)}"
            )
            os.environ["FAKE_PMON_SLEEP"] = "5"
            t0: float = time.monotonic()
            rows = server._get_gpu_processes()
            elapsed: float = time.monotonic() - t0
            assert (
                len(rows) == 3
                and all((r["gpu_util_percent"] is None for r in rows))
                and (elapsed < 4)
            ), (
                f"stalled pmon: process list kept, utilization None, well under the browser's 5 s: {len(rows)} rows in {elapsed:.1f}s"
            )
        finally:
            os.environ["PATH"] = orig_path
            os.environ.pop("FAKE_PMON_SLEEP", None)
