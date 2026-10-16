"""Integration test for the dashboard server.

Starts the server (which auto-starts Glances), pings all API endpoints,
triggers a history record, and checks consistency between live and
historical data.

Usage: uv run python test.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# Expected response shapes per endpoint (path -> validator)
SCHEMAS: dict[str, object] = {
    "/api/4/cpu": lambda d: isinstance(d, dict) and "total" in d,
    "/api/4/mem": lambda d: (
        isinstance(d, dict) and {"used", "total", "percent"} <= d.keys()
    ),
    "/api/4/memswap": lambda d: (
        isinstance(d, dict) and {"used", "total", "percent"} <= d.keys()
    ),
    "/api/4/network": lambda d: (
        isinstance(d, list) and all("interface_name" in e for e in d)
    ),
    "/api/4/diskio": lambda d: isinstance(d, list) and all("disk_name" in e for e in d),
    "/api/4/fs": lambda d: (
        isinstance(d, list) and all({"device_name", "mnt_point"} <= e.keys() for e in d)
    ),
    "/api/4/sensors": lambda d: isinstance(d, list),
    "/api/4/load": lambda d: (
        isinstance(d, dict) and {"min1", "min5", "min15"} <= d.keys()
    ),
    "/api/4/gpu": lambda d: isinstance(d, list),
    # The heavy fields are left out here and served by /api/history/field/<name>.
    "/api/history": lambda d: (
        isinstance(d, dict) and {"records", "count", "skipped_lines"} <= d.keys()
        and not any(k in r for r in d["records"] for k in ("temps", "processes", "ip"))
    ),
    "/api/history/field/temps": lambda d: (
        isinstance(d, dict) and d.get("field") == "temps" and isinstance(d.get("records"), list)
        and all(set(r.keys()) <= {"timestamp", "temps"} for r in d["records"])
    ),
    "/api/history/field/processes": lambda d: (
        isinstance(d, dict) and d.get("field") == "processes" and isinstance(d.get("records"), list)
        and all(set(r.keys()) <= {"timestamp", "processes"} for r in d["records"])
    ),
    "/api/cpu-info": lambda d: (
        isinstance(d, dict) and "model" in d and "logical_cores" in d
    ),
    "/api/gpu-info": lambda d: isinstance(d, list),
    "/api/ip-info": lambda d: (
        isinstance(d, dict) and {"lan", "public", "tailscale"} <= d.keys()
    ),
    # The sanitized route: never a raw cmdline.
    "/api/processlist": lambda d: (
        isinstance(d, list) and not any("cmdline" in p for p in d if isinstance(p, dict))
    ),
    "/api/history/settings": lambda d: (
        isinstance(d, dict)
        and {
            "record_interval",
            "sample_duration",
            "sample_count",
            "ping_enabled",
            "processes_enabled",
            "process_count",
            "defaults",
            "password_configured",
        }
        <= d.keys()
        and {"processes_enabled", "process_count"} <= d["defaults"].keys()
    ),
    "/api/ping": lambda d: isinstance(d, dict) and ("disabled" in d or "targets" in d),
    "/api/gpu-processes": lambda d: isinstance(d, list),
    "/api/disk-processes": lambda d: (
        isinstance(d, list) or (isinstance(d, dict) and ("error" in d or "status" in d))
    ),
    "/api/network-processes": lambda d: (
        isinstance(d, list) or (isinstance(d, dict) and ("error" in d or "status" in d))
    ),
    "/api/mail": lambda d: (
        isinstance(d, dict)
        and {"status", "count", "messages"} <= d.keys()
        and isinstance(d["messages"], list)
    ),
    "/api/storage-health": lambda d: (
        isinstance(d, dict) and {"status", "age_sec", "path"} <= d.keys()
    ),
    "/api/sensors": lambda d: (
        isinstance(d, dict) and {"status", "age_sec", "path"} <= d.keys()
    ),
    "/config.json": lambda d: isinstance(d, dict),
}

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"

passes = 0
fails = 0


def ok(label: str, detail: str = "") -> None:
    global passes
    passes += 1
    suffix = f"  {detail}" if detail else ""
    print(f"  {GREEN}PASS{RESET}  {label}{suffix}")


def fail(label: str, detail: str = "") -> None:
    global fails
    fails += 1
    suffix = f"  {detail}" if detail else ""
    print(f"  {RED}FAIL{RESET}  {label}{suffix}")


def describe(data: object) -> str:
    """One-line summary of a response body for diagnostics."""
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        keys = ", ".join(sorted(data.keys())[:6])
        if len(data.keys()) > 6:
            keys += ", ..."
        return f"dict{{{keys}}}"
    return repr(data)[:80]


def check(condition: bool, label: str, detail: str = "") -> bool:
    if condition:
        ok(label, detail)
    else:
        fail(label, detail)
    return condition


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def wait_for_server(base: str, proc: subprocess.Popen, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Check if process exited early (crash)
        if proc.poll() is not None:
            return False
        try:
            r = httpx.get(f"{base}/", timeout=2)
            if r.status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(0.5)
    return False


def _history_line(age_hours: float, pad: int = 0) -> str:
    """Build a history JSONL line with a timestamp age_hours in the past."""
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    record: dict[str, object] = {"timestamp": ts.isoformat(), "cpu": {"avg": 1.0}}
    if pad:
        record["pad"] = "x" * pad
    return json.dumps(record) + "\n"


def test_read_history_unit() -> None:
    """Unit tests for server._read_history tail-reading (no server process needed)."""
    print(f"\n{BOLD}_read_history unit tests{RESET}\n")
    import server

    orig_history_file = server.HISTORY_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "history.jsonl"
            server.HISTORY_FILE = hist

            def read(
                hours: float, initial: int = 1024 * 1024
            ) -> tuple[list[dict], int]:
                result = server._read_history(hours, initial_read_size=initial)
                if isinstance(result, str):
                    raise AssertionError(f"_read_history failed: {result}")
                records, skipped, _truncated = result
                return records, skipped

            # 1. Missing, empty, and newline-only files
            check(read(24) == ([], 0), "missing file -> ([], 0)")
            hist.write_text("")
            check(read(24) == ([], 0), "empty file -> ([], 0)")
            hist.write_text("\n")
            check(read(24) == ([], 0), "newline-only file -> ([], 0)")

            # 2. Old + in-range records, single read
            hist.write_text(
                "".join(_history_line(100 - i) for i in range(10))  # 100h..91h old
                + "".join(_history_line(5 - i) for i in range(5))  # 5h..1h old
            )
            records, skipped = read(24)
            check(
                len(records) == 5 and skipped == 0,
                "old + in-range -> only in-range, none skipped",
                describe(records),
            )
            timestamps = [r["timestamp"] for r in records]
            check(timestamps == sorted(timestamps), "records are chronological")

            # 3. Doubling: in-range records extend beyond the initial window
            hist.write_text("".join(_history_line(20 - i, pad=100) for i in range(20)))
            records, skipped = read(24, initial=256)
            check(
                len(records) == 20 and skipped == 0,
                "window doubles until range is covered",
                describe(records),
            )

            # 4. Torn last line (no trailing newline) is skipped and counted
            hist.write_text(
                _history_line(30) + _history_line(2) + '{"timestamp": "2026-'
            )
            records, skipped = read(24)
            check(
                len(records) == 1 and skipped == 1,
                "torn last line skipped and counted",
                describe(records),
            )

            # 5. Malformed lines mid-file are skipped and counted, neighbors kept
            hist.write_text(_history_line(3) + "not json\n" + "42\n" + _history_line(2))
            records, skipped = read(24)
            check(
                len(records) == 2 and skipped == 2,
                "malformed mid-file lines skipped and counted",
                describe(records),
            )

            # 6. A far-past record between in-range ones is excluded, not truncating
            hist.write_text(
                _history_line(4)
                + _history_line(3)
                + _history_line(90000)  # bogus ancient timestamp mid-tail
                + _history_line(2)
                + _history_line(1)
            )
            records, skipped = read(24)
            check(
                len(records) == 4 and skipped == 0,
                "ancient mid-tail record excluded without truncation",
                describe(records),
            )

            # 7. Record larger than the initial window (no newline in window)
            hist.write_text(_history_line(30) + _history_line(1, pad=2000))
            records, skipped = read(24, initial=256)
            check(
                len(records) == 1
                and len(records[0].get("pad", "")) == 2000
                and skipped == 0,
                "record larger than window parsed after growth",
                describe(records),
            )

            # 8. Speed: large file of old records, small in-range tail
            with open(hist, "w") as f:
                f.writelines(_history_line(200, pad=400) for _ in range(100_000))
                f.writelines(_history_line(23.5 * (144 - i) / 144) for i in range(144))
            file_mb = hist.stat().st_size / 1e6
            t0 = time.monotonic()
            records, skipped = read(24)
            elapsed = time.monotonic() - t0
            check(
                len(records) == 144 and skipped == 0,
                "large-file tail read returns in-range records",
                describe(records),
            )
            check(
                elapsed < 0.5,
                "large-file tail read is fast",
                f"{elapsed * 1000:.1f}ms for {file_mb:.0f}MB file",
            )

            # 9. Byte cap: a range wider than what fits in max_bytes is truncated,
            #    not read to the start of a huge file.
            hist.write_text(
                "".join(_history_line(i * 0.001, pad=500) for i in range(2000))
            )
            records, skipped, truncated = server._read_history(
                24, initial_read_size=4096, max_bytes=64 * 1024
            )
            check(
                truncated and len(records) > 0,
                "byte cap trips truncated=True with partial recent records",
                f"{len(records)} records, truncated={truncated}",
            )
            # Within-cap read of the same file is not truncated.
            _, _, truncated_full = server._read_history(
                24, initial_read_size=1024 * 1024
            )
            check(not truncated_full, "within-cap read is not truncated")
    finally:
        server.HISTORY_FILE = orig_history_file


def test_null_metrics_unit() -> None:
    """Unit tests for null-safe metric handling (no server/Glances/GPU needed).

    Regression guard: Glances can report a datacenter GPU's metrics (e.g. a Tesla
    P40's) as null, and `round(None)` once crashed the whole recorder every cycle.
    """
    print(f"\n{BOLD}null-metric unit tests{RESET}\n")
    import server

    inf = float("inf")
    nan = float("nan")

    # _num: numbers pass through; None/str/bool/non-finite -> None
    check(server._num(3.5) == 3.5, "_num(3.5) -> 3.5")
    check(server._num(0) == 0.0, "_num(0) -> 0.0 (real zero kept)")
    check(server._num(None) is None, "_num(None) -> None")
    check(server._num("x") is None, "_num(str) -> None")
    check(server._num(True) is None, "_num(bool) -> None (no 1/0 masquerade)")
    check(
        server._num(inf) is None and server._num(nan) is None, "_num(inf/nan) -> None"
    )

    # _round: preserves None, never raises on None (the original crash)
    check(server._round(None, 1) is None, "_round(None, 1) -> None (no crash)")
    check(server._round(3.14159, 1) == 3.1, "_round(3.14159, 1) -> 3.1")
    check(server._round("x", 1) is None, "_round(str, 1) -> None")

    # compute_stats: drops None/non-finite; empty -> all None (not fake 0)
    check(
        server.compute_stats([]) == {"avg": None, "min": None, "max": None},
        "compute_stats([]) -> all None (empty window is unknown, not 0)",
    )
    check(
        server.compute_stats([None, None]) == {"avg": None, "min": None, "max": None},
        "compute_stats([None, None]) -> all None (no crash on None samples)",
    )
    check(
        server.compute_stats([0.0, None, 2.0]) == {"avg": 1.0, "min": 0.0, "max": 2.0},
        "compute_stats drops None, keeps real 0.0",
    )

    # aggregate_cpu_cores: each core averaged over the burst, then min/median/max
    # across those per-core averages; None samples dropped; empty -> None (omitted).
    check(
        server.aggregate_cpu_cores({0: [10.0, 10.0], 1: [50.0, 50.0], 2: [90.0, 90.0]})
        == {"min": 10.0, "median": 50.0, "max": 90.0},
        "aggregate_cpu_cores: 3 steady cores -> min/median/max across cores",
    )
    check(
        server.aggregate_cpu_cores({0: [20.0, None], 1: [None, 60.0]})
        == {"min": 20.0, "median": 40.0, "max": 60.0},
        "aggregate_cpu_cores: drops None samples within a core's burst",
    )
    check(
        server.aggregate_cpu_cores({0: [None], 1: []}) is None,
        "aggregate_cpu_cores: no finite samples -> None (key omitted)",
    )

    # missing_core_metrics: a key present but all-null still counts as missing,
    # since a Glances brownout writes {"avg": None, ...} rather than omitting it.
    check(
        server.missing_core_metrics(
            {"cpu": {"avg": 5.0}, "memory": {"percent": 40.0}}
        )
        == [],
        "missing_core_metrics: full record -> nothing missing",
    )
    check(
        server.missing_core_metrics({"memory": {"percent": 40.0}}) == ["cpu"],
        "missing_core_metrics: absent cpu key -> missing",
    )
    check(
        server.missing_core_metrics(
            {"cpu": {"avg": None, "min": None, "max": None}, "memory": {"percent": 1.0}}
        )
        == ["cpu"],
        "missing_core_metrics: cpu present but all-null -> missing (brownout case)",
    )
    check(
        server.missing_core_metrics({}) == ["cpu", "memory"],
        "missing_core_metrics: empty record -> both missing",
    )

    # _parse_gpu_stats: real line + one with [N/A] fields (field -> None, row kept)
    stats = server._parse_gpu_stats(
        "0, 42, 1024, 24576, 50, 120.5\n1, [N/A], [N/A], 24576, [N/A], [N/A]"
    )
    check(
        len(stats) == 2
        and stats[0]
        == {
            "index": 0,
            "util_percent": 42.0,
            "memory_used_mb": 1024.0,
            "memory_total_mb": 24576.0,
            "temperature": 50.0,
            "power_draw": 120.5,
            # an older 6-column line: the newer columns are absent, not an error
            "temperature_memory": None,
            "throttle_sw_thermal_us": None,
            "throttle_hw_thermal_us": None,
        }
        and stats[1]["index"] == 1
        and stats[1]["util_percent"] is None
        and stats[1]["temperature"] is None
        and stats[1]["power_draw"] is None,
        "_parse_gpu_stats: [N/A] fields -> None, row kept (index required)",
        describe(stats),
    )

    # _parse_gpu_stats: the full 9-column line (mem temp + thermal slowdown
    # counters). The P40 reports memory temp "N/A" (no brackets) and its sw
    # counter "[N/A]"; the V100 reads all of them.
    stats = server._parse_gpu_stats(
        "0, 0, 0, 24576, 22, 9.94, N/A, [N/A], 0\n"
        "1, 97, 30000, 32768, 79, 199.5, 86, 2391391956, 0"
    )
    check(
        len(stats) == 2
        and stats[0]["temperature"] == 22.0
        and stats[0]["temperature_memory"] is None
        and stats[0]["throttle_sw_thermal_us"] is None
        and stats[0]["throttle_hw_thermal_us"] == 0.0
        and stats[1]["temperature_memory"] == 86.0
        and stats[1]["throttle_sw_thermal_us"] == 2391391956.0,
        "_parse_gpu_stats: 9 columns, P40 memory N/A -> None",
        describe(stats),
    )
    # ... and into the history: counters in seconds, "GPU N mem" only when readable
    recs = server.build_gpu_records(stats, [])
    check(
        recs[0]["throttle_sw_thermal_s"] is None
        and recs[0]["throttle_hw_thermal_s"] == 0.0
        and recs[1]["throttle_sw_thermal_s"] == 2391.4
        and json.loads(json.dumps(recs)) == recs,
        "build_gpu_records: thermal slowdown counters us -> s, None kept",
        describe(recs),
    )
    check(
        server.gpu_temps(stats, [])
        == {"GPU 0": 22.0, "GPU 1": 79.0, "GPU 1 mem": 86.0},
        "gpu_temps: 'GPU N mem' when readable, no key for the P40's N/A",
        describe(server.gpu_temps(stats, [])),
    )

    # build_gpu_records: nvidia-smi with all metric fields [N/A] (P40-via-nvidia
    # edge) -> full record, fields None, no crash, JSON-safe.
    na_stats = [
        {
            "index": 0,
            "util_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "temperature": None,
        }
    ]
    recs = server.build_gpu_records(na_stats, [])
    check(
        len(recs) == 1
        and recs[0]["index"] == 0
        and recs[0]["util_percent"] is None
        and recs[0]["mem_used_gb"] is None
        and recs[0]["mem_percent"] is None
        and json.loads(json.dumps(recs)) == recs,
        "build_gpu_records(nvidia N/A) -> record with None fields, JSON-safe",
        describe(recs[0] if recs else {}),
    )

    # build_gpu_records: real nvidia-smi stats -> computed util + memory
    recs = server.build_gpu_records(
        [
            {
                "index": 0,
                "util_percent": 42.0,
                "memory_used_mb": 1024.0,
                "memory_total_mb": 24576.0,
                "temperature": 50.0,
            }
        ],
        [],
    )
    check(
        recs[0]["util_percent"] == 42.0
        and recs[0]["mem_used_gb"] == 1.0
        and recs[0]["mem_total_gb"] == 24.0
        and recs[0]["mem_percent"] == 4.2,
        "build_gpu_records(nvidia-smi) -> computed util + memory",
        describe(recs[0]),
    )

    # build_gpu_records: nvidia util missing -> falls back to Glances proc
    recs = server.build_gpu_records(
        [
            {
                "index": 0,
                "util_percent": None,
                "memory_used_mb": 1024.0,
                "memory_total_mb": 24576.0,
            }
        ],
        [{"gpu_id": "nvidia0", "proc": 55.0}],
    )
    check(
        recs[0]["util_percent"] == 55.0 and recs[0]["mem_total_gb"] == 24.0,
        "build_gpu_records: nvidia util None -> Glances proc fallback",
        describe(recs[0]),
    )

    # build_gpu_records: no nvidia-smi -> Glances util only, memory None
    recs = server.build_gpu_records([], [{"gpu_id": "nvidia0", "proc": 30.0}])
    check(
        recs[0]["util_percent"] == 30.0 and recs[0]["mem_total_gb"] is None,
        "build_gpu_records(Glances only) -> util from proc, memory None",
        describe(recs[0]),
    )

    # gpu_temps: nvidia-smi primary; key emitted even when temp is None (a gap)
    check(
        server.gpu_temps([{"index": 0, "temperature": 23.0}], []) == {"GPU 0": 23.0}
        and server.gpu_temps([{"index": 0, "temperature": None}], [])
        == {"GPU 0": None},
        "gpu_temps: value or None-but-present key",
    )

    # aggregate_ping: loss fraction + stats; full outage -> null stats, loss 1.0
    pings = server.aggregate_ping(
        {"8.8.8.8": [], "1.1.1.1": [10.0, 20.0], "9.9.9.9": [30.0, 30.0, 30.0]},
        {"8.8.8.8": 5, "1.1.1.1": 5, "9.9.9.9": 5},
    )
    check(
        pings["8.8.8.8"] == {"avg": None, "min": None, "max": None, "loss": 1.0},
        "aggregate_ping: all-timeout -> null stats + loss 1.0",
        describe(pings["8.8.8.8"]),
    )
    check(
        pings["1.1.1.1"] == {"avg": 15.0, "min": 10.0, "max": 20.0, "loss": 0.6},
        "aggregate_ping: 2/5 succeeded -> stats + loss 0.6",
        describe(pings["1.1.1.1"]),
    )
    check(
        pings["9.9.9.9"] == {"avg": 30.0, "min": 30.0, "max": 30.0, "loss": 0.4},
        "aggregate_ping: 3/5 succeeded -> loss 0.4",
        describe(pings["9.9.9.9"]),
    )


def test_process_history_unit() -> None:
    """build_process_records / aggregate_cpu_freq: pure record shaping."""
    print(f"\n{BOLD}Unit: per-process history and CPU clocks{RESET}\n")
    import server

    def proc(pid: int, cpu: float, rss: int, name: str = "x") -> dict:
        return {"pid": pid, "name": name, "username": "u", "status": "S",
                "cpu_percent": cpu, "memory_info": {"rss": rss}, "num_threads": 2}

    # pids above the kernel's pid_max ceiling (2**22) can never exist, so the
    # sanitizer falls back to Glances' name and /proc yields no ppid/create_time.
    P = 2**31 - 100
    procs = [proc(P + 1, 50.0, 10 << 20, "hog"), proc(P + 2, 1.0, 900 << 20, "fat"),
             proc(P + 3, 0.5, 1 << 20, "idle"), proc(P + 4, 20.0, 5 << 20, "mid"),
             {"pid": P + 5, "name": "nomem", "cpu_percent": 0.0},
             {"pid": True, "cpu_percent": 99}, "junk"]
    gpu = [{"pid": P + 3, "gpu_index": 1, "gpu_memory_mib": 100.0, "name": "idle"},
           {"pid": P + 3, "gpu_index": 0, "gpu_memory_mib": 50.0, "name": "idle"},
           {"pid": P + 99, "gpu_index": 0, "gpu_memory_mib": 7.0, "name": "ghost"}]
    rows = server.build_process_records(procs, gpu, 2)
    by_pid = {r["pid"]: r for r in rows}
    check(
        [r["pid"] for r in rows] == [P + 1, P + 4, P + 2, P + 3, P + 99]
        and by_pid[P + 1]["name"] == "hog"
        and by_pid[P + 2]["rss_mb"] == 900.0
        and by_pid[P + 3]["gpu_mem_mib"] == 150.0
        and by_pid[P + 3]["gpus"] == [0, 1]
        and "gpus" not in by_pid[P + 1]
        and "cpu_percent" not in by_pid[P + 99]
        and by_pid[P + 99]["gpu_mem_mib"] == 7.0
        and all("ppid" not in r and "create_time" not in r for r in rows)  # pids gone -> omitted
        and all(v is not None for r in rows for v in r.values())  # sparse: no nulls
        and json.loads(json.dumps(rows)) == rows,
        "build_process_records: top-2 cpu + top-2 rss + GPU holders, sorted, GPU-only last",
        describe(rows),
    )
    check(
        server.build_process_records([], [], 5) == []
        and server.build_process_records(procs[:1], [], 5)[0]["pid"] == P + 1,
        "build_process_records: empty and single",
    )
    nomem = {r["pid"]: r for r in server.build_process_records(procs, [], 5)}[P + 5]
    check("rss_mb" not in nomem and nomem["cpu_percent"] == 0.0,
          "build_process_records: missing memory_info -> no rss_mb key, cpu 0.0 kept", describe(nomem))
    # disk rates from Glances' cumulative io_counters; zero / unreadable -> no key
    busy = proc(P + 6, 2.0, 1 << 20, "cp")
    busy.update({"io_counters": [10 * 1024 * 1024, 4 * 1024 * 1024, 0, 4 * 1024 * 1024, 1], "time_since_update": 5.0})
    quiet = proc(P + 7, 1.0, 1 << 20, "idle")
    quiet.update({"io_counters": [5, 5, 5, 5, 1], "time_since_update": 5.0})
    nodata = proc(P + 8, 1.0, 1 << 20, "noio")
    nodata.update({"io_counters": [0, 0, 0, 0, 0], "time_since_update": 5.0})
    io = {r["pid"]: r for r in server.build_process_records([busy, quiet, nodata], [], 5)}
    check(
        io[P + 6]["disk_read_kbps"] == 2048.0 and "disk_write_kbps" not in io[P + 6]
        and "disk_read_kbps" not in io[P + 7] and "disk_write_kbps" not in io[P + 7]
        and "disk_read_kbps" not in io[P + 8],
        "build_process_records: disk KB/s from io_counters, zero and unreadable omitted",
        describe([io[P + 6], io[P + 7], io[P + 8]]),
    )
    f = server.aggregate_cpu_freq([1200.0, 3600.0, None, 2400.0, "x"], {"min_mhz": 1200.0, "max_mhz": 3600.0})
    check(
        f == {"min": 1200.0, "median": 2400.0, "avg": 2400.0, "max": 3600.0, "min_mhz": 1200.0, "max_mhz": 3600.0},
        "aggregate_cpu_freq: pooled min/median/avg/max, junk dropped, limits carried",
        describe(f),
    )
    check(server.aggregate_cpu_freq([]) is None and server.aggregate_cpu_freq([None]) is None,
          "aggregate_cpu_freq: nothing readable -> None")
    me = server.build_process_records([proc(os.getpid(), 1.0, 1 << 20, "me")], [], 1)[0]
    check(
        me.get("ppid") == os.getppid() and isinstance(me.get("create_time"), float)
        and abs(me["create_time"] - time.time()) < 86400,
        "build_process_records: ppid/create_time from /proc for a live pid",
        describe(me),
    )


def test_dedupe_fs_unit() -> None:
    """Unit tests for filesystem de-dup by backing device (no server needed).

    Guards the fix for systemd PrivateTmp bind mounts (/tmp, /var/tmp) that mirror
    / as duplicate 'drives'. Device names are real (/dev/dm-0 etc.) so realpath
    leaves them unchanged and the test is deterministic without symlinks.
    """
    print(f"\n{BOLD}fs-dedup unit tests{RESET}\n")
    import server

    def _mounts(entries: list[dict]) -> list[str]:
        return [e["mnt_point"] for e in server._dedupe_fs_by_device(entries)]

    # Phantom PrivateTmp mounts: same device + size + used as / -> only / kept,
    # even though /tmp is listed first (shortest, non-empty mount wins).
    phantom = [
        {"device_name": "/dev/dm-0", "mnt_point": "/tmp", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/var/tmp", "size": 200, "used": 148},
    ]
    check(
        _mounts(phantom) == ["/"],
        "dedupe: /tmp + /var/tmp (same dev/size/used as /) collapse to /",
        describe(_mounts(phantom)),
    )

    # Distinct devices -> all kept, original order preserved.
    distinct = [
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
        {"device_name": "/dev/sda2", "mnt_point": "/boot", "size": 2, "used": 1},
        {"device_name": "/dev/sdb1", "mnt_point": "/mnt/scratch", "size": 880, "used": 28},
    ]
    check(
        _mounts(distinct) == ["/", "/boot", "/mnt/scratch"],
        "dedupe: distinct devices all kept, order preserved",
        describe(_mounts(distinct)),
    )

    # Same device but different `used` -> NOT the same fs, both kept (over-collapse guard).
    subvol = [
        {"device_name": "/dev/dm-1", "mnt_point": "/data/a", "size": 500, "used": 100},
        {"device_name": "/dev/dm-1", "mnt_point": "/data/b", "size": 500, "used": 200},
    ]
    check(
        _mounts(subvol) == ["/data/a", "/data/b"],
        "dedupe: same device, different used -> both kept (no over-collapse)",
        describe(_mounts(subvol)),
    )

    # Same device, `used` a few blocks apart (a busy root fs statvfs'd twice, for
    # '/' and PrivateTmp's '/tmp'): within 0.1% of size it is one fs, '/' wins.
    GB = 1024**3
    jitter = [
        {"device_name": "/dev/nvme0n1p2", "mnt_point": "/tmp", "size": 900 * GB, "used": 400 * GB + 65536},
        {"device_name": "/dev/nvme0n1p2", "mnt_point": "/", "size": 900 * GB, "used": 400 * GB},
    ]
    check(
        _mounts(jitter) == ["/"],
        "dedupe: same device, used differs by a few KB -> one fs, shortest mount wins",
        describe(_mounts(jitter)),
    )

    # Empty device_name -> always kept (nothing to dedupe against).
    no_dev = [{"device_name": "", "mnt_point": "/weird", "size": 1, "used": 0}]
    check(
        _mounts(no_dev) == ["/weird"],
        "dedupe: entry with empty device_name kept as-is",
        describe(_mounts(no_dev)),
    )

    # Blank mount_point sharing a device/size/used with / -> / wins, blank dropped.
    blank = [
        {"device_name": "/dev/dm-0", "mnt_point": "", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
    ]
    check(
        _mounts(blank) == ["/"],
        "dedupe: real / wins over a blank-mount entry on the same fs",
        describe(_mounts(blank)),
    )


def test_mail_and_health_unit() -> None:
    """Unit tests for the two file readers behind the System Mail and Hardware cards."""
    print(f"\n{BOLD}mail + hardware health unit tests{RESET}\n")
    import base64

    import server

    subject_utf8 = base64.b64encode("SMART error ✓ <b>&\"'".encode()).decode()
    # postfix-shaped mbox: From_ line, headers, blank line, body, blank separator line.
    # Body lines starting with "From " are quoted as ">From " (mboxo), which is what
    # postfix local(8) writes and what the stdlib mailbox reader relies on.
    fixture = (
        "From root@box  Mon Sep 14 08:00:00 2026\n"
        "Date: Mon, 14 Sep 2026 08:00:00 -0400\nFrom: root <root@box>\nTo: root\n"
        "Subject: cron output\n\n>From here quoted\n>From also quoted\n\n"
        "From root@box  Sun Sep 20 23:00:00 2026\n"
        "Date: Sun, 20 Sep 2026 23:00:00 -0400\nFrom: smartd <root@box>\nTo: root\n"
        f"Subject: =?utf-8?b?{subject_utf8}?=\n\nsmartd says hi\n\n"
        "From root@box  Sun Sep 20 23:30:00 2026\n"
        "From: root <root@box>\nTo: root\nSubject: no date\n\nbody\n\n"
        "From root@box  Sun Sep 20 23:45:00 2026\n"
        "Date: not a date at all\nFrom: root <root@box>\nTo: root\nSubject: bad date\n\nbody\n\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        spool = os.path.join(tmp, "spool")
        with open(spool, "w", encoding="utf-8") as f:
            f.write(fixture)
        m = server._get_mail(spool)
        check(m["status"] == "ok" and m["count"] == 4, "mail: fixture spool counts 4", str(m["count"]))
        check(
            [x["subject"] for x in m["messages"]][:3] == ["bad date", "no date", "SMART error ✓ <b>&\"'"],
            "mail: newest first, RFC 2047 subject decoded",
            str([x["subject"] for x in m["messages"]]),
        )
        check(
            [x["date_epoch"] is None for x in m["messages"]] == [True, True, False, False],
            "mail: garbage or missing Date: gives null epoch, real ones parse",
            str([x["date_epoch"] for x in m["messages"]]),
        )
        check(
            server._get_mail(spool, limit=1)["count"] == 4
            and len(server._get_mail(spool, limit=1)["messages"]) == 1,
            "mail: limit trims messages but not count",
        )
        check(
            server._get_mail(os.path.join(tmp, "nope"))["status"] == "missing",
            "mail: missing spool is 'missing', not an exception",
        )
        check(
            server._get_mail(tmp)["status"] == "error",
            "mail: a directory is 'error', not an exception",
        )
        # One unparseable message must neither vanish nor take the card down: it
        # stays in the list as a red "unreadable" row. The stdlib parser is
        # lenient (the bad/missing dates above don't raise), so force a raise.
        real_mfb = server.email.message_from_bytes

        def boom(data, *a, **k):
            if b"smartd says hi" in data:  # the SMART-error message's body
                raise ValueError("simulated unparseable message")
            return real_mfb(data, *a, **k)

        server.email.message_from_bytes = boom
        try:
            mp = server._get_mail(spool)
        finally:
            server.email.message_from_bytes = real_mfb
        bad = [x for x in mp["messages"] if x.get("error")]
        check(
            mp["status"] == "ok"
            and mp["count"] == 4
            and mp["unreadable"] == 1
            and len(mp["messages"]) == 4
            and len(bad) == 1
            and bad[0]["error"] == "ValueError"
            and bad[0]["subject"].startswith("unreadable message"),
            "mail: an unparseable message is a red row (unreadable=1), the rest still show",
            describe(mp),
        )
        state = os.path.join(tmp, "state.json")
        check(
            server._get_storage_health(state)["status"] == "missing",
            "health: missing snapshot is 'missing'",
        )
        with open(state, "w") as f:
            json.dump({"ts": time.time() - 60, "alarms": ["md0: DEGRADED"], "drives": []}, f)
        h = server._get_storage_health(state)
        check(
            h["status"] == "ok" and h["alarms"] == ["md0: DEGRADED"] and 0 <= h["age_sec"] < 120,
            "health: fresh snapshot passes alarms through with age",
            describe(h),
        )
        with open(state, "w") as f:
            json.dump({"ts": time.time() - 2 * 3600, "alarms": []}, f)
        check(
            server._get_storage_health(state)["status"] == "stale",
            "health: two-hour-old snapshot is 'stale'",
        )
        with open(state, "w") as f:
            json.dump({"ts": float("nan"), "alarms": []}, f)  # json allows NaN
        check(
            server._get_storage_health(state)["status"] == "error",
            "health: non-finite ts is 'error', not an exception",
        )
        with open(state, "w") as f:
            f.write("{not json")
        e = server._get_storage_health(state)
        check(
            e["status"] == "error" and e["detail"] == "JSONDecodeError",
            "health: corrupt snapshot is 'error' with the exception name",
            describe(e),
        )
        with open(state, "w") as f:
            json.dump({"status": "hacked", "ts": time.time()}, f)
        check(
            server._get_storage_health(state)["status"] == "ok",
            "health: the file cannot override status",
        )


def main() -> int:
    global passes, fails
    test_read_history_unit()
    test_null_metrics_unit()
    test_process_history_unit()
    test_dedupe_fs_unit()
    test_mail_and_health_unit()
    port = free_port()
    glances_port = free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"\n{BOLD}Starting server on :{port} (Glances :{glances_port}){RESET}\n")
    log_path = Path(__file__).parent / "logs" / "test-server.log"
    log_path.parent.mkdir(exist_ok=True)
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "server.py",
            "--port",
            str(port),
            "--glances-port",
            str(glances_port),
        ],
        cwd=Path(__file__).parent,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group so we can kill server + Glances together
    )

    try:
        if not wait_for_server(base, proc):
            fail("server startup")
            # Show last lines of server output for diagnosis
            log_file.flush()
            try:
                tail = log_path.read_text().strip().split("\n")[-20:]
                print(f"\n  {BOLD}Server output (last {len(tail)} lines):{RESET}")
                for line in tail:
                    print(f"    {line}")
            except OSError:
                print("\n  No server output captured")
            return 1
        ok("server startup")

        # ── Ping all endpoints ──────────────────────────────────────
        print(f"\n{BOLD}Pinging API endpoints{RESET}\n")

        # "/" and "/config.json" are dedicated routes; the last entry is the only
        # thing exercising the /static mount that serves the whole UI.
        static_endpoints = ["/", "/config.json", "/static/app.js"]
        glances_endpoints = [
            "/api/4/cpu",
            "/api/4/mem",
            "/api/4/memswap",
            "/api/4/network",
            "/api/4/diskio",
            "/api/4/fs",
            "/api/4/sensors",
            "/api/4/load",
        ]
        # gpu may 404 if no GPU — treat as optional
        optional_endpoints = ["/api/4/gpu"]
        custom_endpoints = [
            "/api/history",
            "/api/history/field/temps",
            "/api/history/field/processes",
            "/api/cpu-info",
            "/api/ip-info",
            "/api/processlist",
            "/api/history/settings",
            "/api/ping",
            "/api/mail",  # 200 even with no spool; body carries the status
            "/api/storage-health",  # 200 even with no snapshot; status says why
            "/api/sensors",  # same contract, 60 s snapshot
        ]
        # gpu-info needs nvidia-smi — optional
        # disk/network-processes need pidstat/nethogs — optional
        optional_custom = [
            "/api/gpu-info",
            "/api/gpu-processes",
            "/api/disk-processes",
            "/api/network-processes",
        ]

        for path in static_endpoints + glances_endpoints + custom_endpoints:
            try:
                r = httpx.get(f"{base}{path}", timeout=10)
                if check(
                    r.status_code == 200, f"GET {path}", f"status={r.status_code}"
                ):
                    validator = SCHEMAS.get(path)
                    if validator:
                        data = r.json()
                        check(validator(data), f"  format {path}", describe(data))
            except Exception as e:
                fail(f"GET {path}", str(e))

        for path in optional_endpoints + optional_custom:
            try:
                r = httpx.get(f"{base}{path}", timeout=10)
                if r.status_code == 200:
                    ok(f"GET {path}")
                    validator = SCHEMAS.get(path)
                    if validator:
                        data = r.json()
                        check(validator(data), f"  format {path}", describe(data))
                else:
                    print(f"  {YELLOW}SKIP{RESET}  GET {path}  status={r.status_code}")
            except Exception as e:
                print(f"  {YELLOW}SKIP{RESET}  GET {path}  {e}")

        # ── closed doors: raw Glances process views and the API docs ─
        for path in ("/api/4/processlist", "/api/4/all", "/api/4/processes/1", "/docs"):
            try:
                r = httpx.get(f"{base}{path}", timeout=10)
                check(r.status_code == 404, f"GET {path} is 404", f"status={r.status_code}")
            except Exception as e:
                fail(f"GET {path}", str(e))
        # Dot segments must not slip past the deny list. httpx normalises them
        # client-side, so send the raw path over http.client instead.
        import http.client
        from urllib.parse import urlsplit

        host = urlsplit(base)
        for path in ("/api/4/x/../processlist", "/api/4/./all", "/api/4/%2e%2e/4/processlist"):
            try:
                conn = http.client.HTTPConnection(host.hostname, host.port, timeout=10)
                conn.request("GET", path)
                status = conn.getresponse().status
                conn.close()
                check(status == 404, f"GET {path} (raw) is 404", f"status={status}")
            except Exception as e:
                fail(f"GET {path} (raw)", str(e))

        # ── file-backed endpoints are advertised to the frontend ─────
        print(f"\n{BOLD}Endpoint listing{RESET}\n")
        try:
            r = httpx.get(f"{base}/api/list-endpoints", timeout=10)
            advertised = r.json().get("endpoints", []) if r.status_code == 200 else []
            for path in custom_endpoints:
                short = path.removeprefix("/api")
                check(short in advertised, f"GET /api/list-endpoints advertises {short}")
        except Exception as e:
            fail("GET /api/list-endpoints", str(e))

        # ── Trigger history record ──────────────────────────────────
        print(f"\n{BOLD}History record{RESET}\n")

        try:
            r = httpx.post(f"{base}/api/history/record", timeout=30)
            record_ok = check(r.status_code == 200, "POST /api/history/record")
            record_data: dict = r.json() if record_ok else {}
        except Exception as e:
            fail("POST /api/history/record", str(e))
            record_data = {}

        # ── /api/history returns RAW records (no downsampling) ───────
        try:
            r = httpx.get(f"{base}/api/history?hours=24", timeout=30)
            if check(r.status_code == 200, "GET /api/history"):
                h = r.json()
                check(
                    "raw_count" not in h and "max_points" not in h,
                    "history response has no downsampling keys (raw_count/max_points)",
                    describe(h),
                )
                check(
                    h.get("count") == len(h.get("records", [])),
                    "history count == number of raw records returned",
                    f"count={h.get('count')} records={len(h.get('records', []))}",
                )
                check(
                    {"truncated", "recording"} <= h.keys(),
                    "history response keeps truncated + recording",
                    describe(h),
                )
                check(
                    all("ip" not in rec for rec in h.get("records", [])),
                    "GET /api/history strips ip from every record",
                )
        except Exception as e:
            fail("GET /api/history", str(e))

        # ── Raw history export (password-gated: records embed LAN/public/
        # Tailscale IPs, so an unauthenticated bulk export is not offered) ──
        try:
            r = httpx.get(f"{base}/api/history/raw", timeout=30)
            # 403 = password configured and withheld; 503 = no password set up.
            check(
                r.status_code in (403, 503),
                "GET /api/history/raw is gated without a password",
                f"status={r.status_code}",
            )
            r = httpx.get(
                f"{base}/api/history/raw", params={"password": "wrong"}, timeout=30
            )
            check(
                r.status_code in (403, 503),
                "GET /api/history/raw rejects a wrong password",
                f"status={r.status_code}",
            )
        except Exception as e:
            fail("GET /api/history/raw", str(e))

        record: dict = record_data.get("record", {})
        drives_io: list = []

        if record:
            check("cpu" in record, "history has cpu")
            check("memory" in record, "history has memory")
            check("disk_io" in record, "history has disk_io")
            check("drives" in record, "history has drives")
            # `ip` is recorded to disk (see /api/history/raw) but never served
            # unauthenticated.
            check("ip" not in record, "POST record echo strips ip")

            # Verify disk_io stats structure (avg/min/max)
            dio = record.get("disk_io", {})
            for key in ("read_mbps", "write_mbps"):
                stats = dio.get(key, {})
                check(
                    isinstance(stats, dict) and {"avg", "min", "max"} <= stats.keys(),
                    f"disk_io.{key} has avg/min/max",
                )

            # CPU core spread is optional (present only when /api/4/percpu is
            # readable); when present it must be an ordered min<=median<=max.
            if "cpu_cores" in record:
                cc = record["cpu_cores"]
                check(
                    isinstance(cc, dict)
                    and {"min", "median", "max"} <= cc.keys()
                    and cc["min"] <= cc["median"] <= cc["max"],
                    "history cpu_cores has ordered min<=median<=max",
                    describe(cc),
                )

            # CPU clocks are optional (cpufreq sysfs); when present, ordered and in MHz.
            if "cpu_freq" in record:
                cf = record["cpu_freq"]
                check(
                    isinstance(cf, dict)
                    and {"min", "median", "avg", "max"} <= cf.keys()
                    and cf["min"] <= cf["median"] <= cf["max"]
                    and cf["min"] <= cf["avg"] <= cf["max"]
                    and cf["min"] > 0,
                    "history cpu_freq has ordered min<=median/avg<=max",
                    describe(cf),
                )
            # Per-process history: the key is there exactly when the setting is on
            # (off unless this box's config.json enables it).
            try:
                hs = httpx.get(f"{base}/api/history/settings", timeout=10).json()
                on = hs.get("processes_enabled") is True
                check(
                    ("processes" in record) == on
                    and (not on or all({"pid", "name"} <= r.keys() and None not in r.values()
                                       for r in record["processes"])),
                    f"history processes present iff enabled (enabled={on}); rows sparse",
                )
            except Exception as e:
                fail("history processes presence", str(e))

            # Verify per-drive stats structure
            drives_io = dio.get("drives", [])
            for d in drives_io:
                name = d.get("name", "?")
                for key in ("read_mbps", "write_mbps"):
                    stats = d.get(key, {})
                    check(
                        isinstance(stats, dict)
                        and {"avg", "min", "max"} <= stats.keys(),
                        f"disk_io.drives[{name}].{key} has avg/min/max",
                    )
            check(len(drives_io) > 0, "history has per-drive disk_io")
        else:
            fail("history record returned data")

        # ── Consistency: live fs vs history drives ───────────────────
        print(f"\n{BOLD}Live / history consistency{RESET}\n")

        try:
            fs_resp = httpx.get(f"{base}/api/4/fs", timeout=10)
            diskio_resp = httpx.get(f"{base}/api/4/diskio", timeout=10)
            fs_data: list = fs_resp.json() if fs_resp.status_code == 200 else []
            diskio_data: list = (
                diskio_resp.json() if diskio_resp.status_code == 200 else []
            )
        except Exception as e:
            fail("fetch live fs/diskio", str(e))
            fs_data, diskio_data = [], []

        # Build the same mapping the dashboard uses
        device_to_mount: dict[str, str] = {}
        for fs in fs_data:
            dev: str = fs.get("device_name", "")
            mnt: str = fs.get("mnt_point", "")
            if dev and mnt and dev.startswith("/dev/"):
                device_to_mount[dev[5:]] = mnt

        diskio_names = {d.get("disk_name") for d in diskio_data if d.get("disk_name")}

        # Drives that are both in fs and diskio should appear in history
        expected_mounts = {
            device_to_mount[name] for name in device_to_mount if name in diskio_names
        }
        history_drive_names = {d.get("name") for d in drives_io}

        if expected_mounts:
            check(
                expected_mounts == history_drive_names,
                "history drives match live fs ∩ diskio",
                f"expected={sorted(expected_mounts)}  got={sorted(history_drive_names)}",
            )
        else:
            print(f"  {YELLOW}SKIP{RESET}  no drives in both fs and diskio to compare")

        # ── Consistency: live values vs history values ───────────────
        # For each drive in both live and history, the history avg should be
        # close to what we'd compute from live bytes / 1e6 (same conversion).
        # We can't compare exact values (different time windows), but we can
        # verify the order of magnitude is the same (not off by 1024/1000).
        for drive_hist in drives_io:
            name = drive_hist.get("name", "?")
            # Find the diskio device for this mount
            dev_name = None
            for dname, mnt in device_to_mount.items():
                if mnt == name:
                    dev_name = dname
                    break
            if dev_name is None:
                continue

            live_disk = next(
                (d for d in diskio_data if d.get("disk_name") == dev_name), None
            )
            if live_disk is None:
                continue

            live_read = (live_disk.get("read_bytes_rate_per_sec") or 0) / 1e6
            hist_read_avg = drive_hist.get("read_mbps", {}).get("avg", 0)
            live_write = (live_disk.get("write_bytes_rate_per_sec") or 0) / 1e6
            hist_write_avg = drive_hist.get("write_mbps", {}).get("avg", 0)

            # Both should be non-negative
            check(
                live_read >= 0 and hist_read_avg >= 0,
                f"drive {name} read values non-negative",
                f"live={live_read:.4f}  hist_avg={hist_read_avg:.4f}",
            )
            check(
                live_write >= 0 and hist_write_avg >= 0,
                f"drive {name} write values non-negative",
                f"live={live_write:.4f}  hist_avg={hist_write_avg:.4f}",
            )

        # ── Summary ─────────────────────────────────────────────────
        print(f"\n{BOLD}{'─' * 40}{RESET}")
        total = passes + fails
        if fails == 0:
            print(f"{GREEN}{BOLD}All {total} checks passed{RESET}\n")
        else:
            print(f"{RED}{BOLD}{fails} of {total} checks failed{RESET}\n")
        return 1 if fails else 0

    finally:
        log_file.close()
        # Kill the entire process group (server + Glances child)
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
        except (ProcessLookupError, OSError):
            pass  # already dead


if __name__ == "__main__":
    sys.exit(main())
