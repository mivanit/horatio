from __future__ import annotations

import asyncio
import json
import os
import time
import unittest.mock
from typing import Any
from unittest.mock import patch

import httpx

from tests.support import describe


def test_process_history_unit() -> None:
    """build_process_records / aggregate_cpu_freq: pure record shaping."""
    import server

    def proc(pid: int, cpu: float, rss: int, name: str = "x") -> dict[str, Any]:
        return {
            "pid": pid,
            "name": name,
            "username": "u",
            "status": "S",
            "cpu_percent": cpu,
            "memory_info": {"rss": rss},
            "num_threads": 2,
        }

    # pids above the kernel's pid_max ceiling (2**22) can never exist, so the
    # sanitizer falls back to Glances' name and /proc yields no ppid/create_time.
    P: int = 2**31 - 100
    procs: list[Any] = [
        proc(P + 1, 50.0, 10 << 20, "hog"),
        proc(P + 2, 1.0, 900 << 20, "fat"),
        proc(P + 3, 0.5, 1 << 20, "idle"),
        proc(P + 4, 20.0, 5 << 20, "mid"),
        {"pid": P + 5, "name": "nomem", "cpu_percent": 0.0},
        {"pid": True, "cpu_percent": 99},
        "junk",
    ]
    gpu: list[dict[str, float | str] | dict[str, float | str | None]] = [
        {
            "pid": P + 3,
            "gpu_index": 1,
            "gpu_memory_mib": 100.0,
            "name": "idle",
            "gpu_util_percent": 40.0,
        },
        {
            "pid": P + 3,
            "gpu_index": 0,
            "gpu_memory_mib": 50.0,
            "name": "idle",
            "gpu_util_percent": 2.5,
        },
        {
            "pid": P + 99,
            "gpu_index": 0,
            "gpu_memory_mib": 7.0,
            "name": "ghost",
            "gpu_util_percent": None,
        },
    ]
    rows: list[dict[str, Any]] = server.build_process_records(procs, gpu, 2)
    by_pid: dict[int, dict[str, Any]] = {r["pid"]: r for r in rows}
    assert (
        [r["pid"] for r in rows] == [P + 1, P + 4, P + 2, P + 3, P + 99]
        and by_pid[P + 1]["name"] == "hog"
        and (by_pid[P + 2]["rss_mb"] == 900.0)
        and (by_pid[P + 3]["gpu_mem_mib"] == 150.0)
        and (by_pid[P + 3]["gpus"] == [0, 1])
        and (by_pid[P + 3].get("gpu_util_percent") == 42.5)
        and ("gpu_util_percent" not in by_pid[P + 99])
        and ("gpus" not in by_pid[P + 1])
        and ("cpu_percent" not in by_pid[P + 99])
        and (by_pid[P + 99]["gpu_mem_mib"] == 7.0)
        and all(("ppid" not in r and "create_time" not in r for r in rows))
        and all((v is not None for r in rows for v in r.values()))
        and (json.loads(json.dumps(rows)) == rows)
    ), (
        f"build_process_records: top-2 cpu + top-2 rss + GPU holders, sorted, GPU-only last: {describe(rows)}"
    )
    ghosts: dict[int, dict[str, Any]] = {
        r["pid"]: r
        for r in server.build_process_records(
            [proc(P + 96, 1.0, 1 << 20, "listed")],
            [
                {
                    "pid": P + 98,
                    "gpu_index": 0,
                    "gpu_memory_mib": 1.0,
                    "name": "g",
                    "gpu_util_percent": 12.0,
                },
                {
                    "pid": P + 97,
                    "gpu_index": 0,
                    "gpu_memory_mib": 1.0,
                    "name": "z",
                    "gpu_util_percent": 0.0,
                },
                {
                    "pid": P + 96,
                    "gpu_index": 0,
                    "gpu_memory_mib": 1.0,
                    "name": "listed",
                    "gpu_util_percent": 0.0,
                },
            ],
            5,
        )
    }
    assert (
        ghosts[P + 98].get("gpu_util_percent") == 12.0
        and "gpu_util_percent" not in ghosts[P + 97]
        and ("gpu_util_percent" not in ghosts[P + 96])
    ), (
        f"build_process_records: GPU-only rows keep utilization; 0 is omitted (listed or GPU-only): {describe(list(ghosts.values()))}"
    )
    assert (
        server.build_process_records([], [], 5) == []
        and server.build_process_records(procs[:1], [], 5)[0]["pid"] == P + 1
    ), "build_process_records: empty and single"
    nomem: dict[str, Any] = {
        r["pid"]: r for r in server.build_process_records(procs, [], 5)
    }[P + 5]
    assert "rss_mb" not in nomem and nomem["cpu_percent"] == 0.0, (
        f"build_process_records: missing memory_info -> no rss_mb key, cpu 0.0 kept: {describe(nomem)}"
    )
    # disk rates from Glances' cumulative io_counters; zero / unreadable -> no key
    busy: dict[str, Any] = proc(P + 6, 2.0, 1 << 20, "cp")
    busy.update(
        {
            "io_counters": [10 * 1024 * 1024, 4 * 1024 * 1024, 0, 4 * 1024 * 1024, 1],
            "time_since_update": 5.0,
        }
    )
    quiet: dict[str, Any] = proc(P + 7, 1.0, 1 << 20, "idle")
    quiet.update({"io_counters": [5, 5, 5, 5, 1], "time_since_update": 5.0})
    nodata: dict[str, Any] = proc(P + 8, 1.0, 1 << 20, "noio")
    nodata.update({"io_counters": [0, 0, 0, 0, 0], "time_since_update": 5.0})
    io: dict[int, dict[str, Any]] = {
        r["pid"]: r for r in server.build_process_records([busy, quiet, nodata], [], 5)
    }
    assert (
        io[P + 6]["disk_read_kbps"] == 2048.0
        and "disk_write_kbps" not in io[P + 6]
        and ("disk_read_kbps" not in io[P + 7])
        and ("disk_write_kbps" not in io[P + 7])
        and ("disk_read_kbps" not in io[P + 8])
    ), (
        f"build_process_records: disk KB/s from io_counters, zero and unreadable omitted: {describe([io[P + 6], io[P + 7], io[P + 8]])}"
    )
    pm: dict[tuple[int, int], tuple[float | None, float | None]] = server._parse_pmon(
        "# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command \n"
        "# Idx           #    C/G      %      %      %      %      %      %    name \n"
        "    0    1498409     C     97     75      -      -      -      -    python3        \n"
        "    0    1498411     C      -      -      -      -      -      -    python3        \n"
        "    1    1498408     C     87     48      -      -      -      -    python3        \n"
        "garbage line\n"
    )
    assert pm == {
        (0, 1498409): (97.0, 75.0),
        (0, 1498411): (None, None),
        (1, 1498408): (87.0, 48.0),
    }, (
        f"_parse_pmon: (gpu, pid) -> (sm, mem), '-' -> None, headers and junk skipped: {repr(pm)}"
    )
    f: dict[str, float | None] | None = server.aggregate_cpu_freq(
        [1200.0, 3600.0, None, 2400.0, "x"], {"min_mhz": 1200.0, "max_mhz": 3600.0}
    )
    assert f == {
        "min": 1200.0,
        "median": 2400.0,
        "avg": 2400.0,
        "max": 3600.0,
        "min_mhz": 1200.0,
        "max_mhz": 3600.0,
    }, (
        f"aggregate_cpu_freq: pooled min/median/avg/max, junk dropped, limits carried: {describe(f)}"
    )
    assert (
        server.aggregate_cpu_freq([]) is None
        and server.aggregate_cpu_freq([None]) is None
    ), "aggregate_cpu_freq: nothing readable -> None"
    me: dict[str, Any] = server.build_process_records(
        [proc(os.getpid(), 1.0, 1 << 20, "me")], [], 1
    )[0]
    assert (
        me.get("ppid") == os.getppid()
        and isinstance(me.get("create_time"), float)
        and (abs(me["create_time"] - time.time()) < 86400)
    ), (
        f"build_process_records: ppid/create_time from /proc for a live pid: {describe(me)}"
    )


def test_history_process_sources_unit() -> None:
    """A missing Glances process list must not discard available GPU processes."""
    from unittest.mock import Mock

    import server

    pid: int = 2**31 - 1  # cannot be a live process on Linux
    gpu_rows: list[dict[str, int | str]] = [
        {
            "pid": pid,
            "name": "trainer",
            "gpu_index": 0,
            "gpu_memory_mib": 4096,
            "gpu_util_percent": 90,
        }
    ]
    cpu_rows: list[dict[str, Any]] = [
        {
            "pid": pid,
            "name": "trainer",
            "cpu_percent": 20,
            "memory_info": {"rss": 1024**2},
        }
    ]
    responses: dict[str, Any] = {
        "cpu": {"total": 20},
        "percpu": [{"total": 20}],
        "network": [],
        "diskio": [],
        "mem": {"used": 1024, "total": 2048, "percent": 50},
        "memswap": {},
        "gpu": [],
        "sensors": [],
        "load": {},
        "fs": [],
    }
    requests: list[str] = []

    class Client:
        async def __aenter__(self: Client) -> Client:
            return self

        async def __aexit__(self: Client, *args: object) -> None:
            pass

        async def get(self: Client, url: str, **kwargs: Any) -> httpx.Response:
            name: str = url.rsplit("/", 1)[1]
            requests.append(name)
            response: httpx.Response | None = (
                process_reply
                if name == "processlist"
                else httpx.Response(200, json=responses[name])
            )
            assert response is not None
            return response

    async def run() -> None:
        nonlocal process_reply
        gpu: unittest.mock.MagicMock | unittest.mock.AsyncMock
        with (
            patch.multiple(
                server.APP.state,
                create=True,
                history_sample_duration=1,
                history_sample_count=1,
                history_processes_enabled=True,
                history_process_count=10,
                ping_enabled=False,
            ),
            patch.object(server.httpx, "AsyncClient", Client),
            patch.multiple(
                server,
                _read_cpu_freqs=Mock(return_value=[]),
                _get_ip_info=Mock(return_value={}),
                get_gpu_stats=Mock(return_value=[]),
                _get_storage_health=Mock(return_value={"status": "missing"}),
            ),
            patch.object(server, "_get_gpu_processes", return_value=gpu_rows) as gpu,
        ):
            label: str
            status: int
            data: list[Any] | dict[str, Any]
            for label, status, data in (
                ("failed", 500, []),
                ("empty", 200, []),
                ("non-list", 200, {}),
            ):
                process_reply = httpx.Response(status, json=data)
                gpu.reset_mock()
                record: dict[str, Any] = await server.collect_history_sample(
                    "http://fixture"
                )
                rows: list[dict[str, Any]] = record.get("processes", [])
                assert (
                    gpu.call_count == 1
                    and len(rows) == 1
                    and (rows[0].get("gpu_mem_mib") == 4096)
                    and (rows[0].get("gpu_util_percent") == 90)
                    and (rows[0].get("gpus") == [0])
                    and ("cpu_percent" not in rows[0])
                    and (record["cpu"]["avg"] == 20)
                    and (record["memory"]["percent"] == 50)
                ), (
                    f"History: {label} Glances processes preserve GPU rows and other metrics"
                )

            server.APP.state.history_processes_enabled = False
            gpu.reset_mock()
            requests.clear()
            record = await server.collect_history_sample("http://fixture")
            assert (
                gpu.call_count == 0
                and "processes" not in record
                and ("processlist" not in requests)
            ), "History: disabled process recording skips both process sources"

            server.APP.state.history_processes_enabled = True
            process_reply = httpx.Response(200, json=cpu_rows)
            gpu.side_effect = FileNotFoundError("nvidia-smi")
            record = await server.collect_history_sample("http://fixture")
            rows = record.get("processes", [])
            assert (
                len(rows) == 1
                and rows[0].get("cpu_percent") == 20
                and (rows[0].get("rss_mb") == 1)
                and ("gpu_mem_mib" not in rows[0])
            ), "History: missing NVIDIA keeps Glances process rows"

    process_reply: httpx.Response | None = None
    asyncio.run(run())
