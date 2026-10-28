from __future__ import annotations

import json
from typing import Any

from tests.support import describe


def test_null_metrics_unit() -> None:
    """Unit tests for null-safe metric handling (no server/Glances/GPU needed).

    Regression guard: Glances can report a datacenter GPU's metrics (e.g. a Tesla
    P40's) as null, and `round(None)` once crashed the whole recorder every cycle.
    """
    import server

    inf: float = float("inf")
    nan: float = float("nan")

    # _num: numbers pass through; None/str/bool/non-finite -> None
    assert server._num(3.5) == 3.5, "_num(3.5) -> 3.5"
    assert server._num(0) == 0.0, "_num(0) -> 0.0 (real zero kept)"
    assert server._num(None) is None, "_num(None) -> None"
    assert server._num("x") is None, "_num(str) -> None"
    assert server._num(True) is None, "_num(bool) -> None (no 1/0 masquerade)"
    assert server._num(inf) is None and server._num(nan) is None, (
        "_num(inf/nan) -> None"
    )

    # _round: preserves None, never raises on None (the original crash)
    assert server._round(None, 1) is None, "_round(None, 1) -> None (no crash)"
    assert server._round(3.14159, 1) == 3.1, "_round(3.14159, 1) -> 3.1"
    assert server._round("x", 1) is None, "_round(str, 1) -> None"

    # compute_stats: drops None/non-finite; empty -> all None (not fake 0)
    assert server.compute_stats([]) == {"avg": None, "min": None, "max": None}, (
        "compute_stats([]) -> all None (empty window is unknown, not 0)"
    )
    assert server.compute_stats([None, None]) == {
        "avg": None,
        "min": None,
        "max": None,
    }, "compute_stats([None, None]) -> all None (no crash on None samples)"
    assert server.compute_stats([0.0, None, 2.0]) == {
        "avg": 1.0,
        "min": 0.0,
        "max": 2.0,
    }, "compute_stats drops None, keeps real 0.0"

    # aggregate_cpu_cores: each core averaged over the burst, then min/median/max
    # across those per-core averages; None samples dropped; empty -> None (omitted).
    assert server.aggregate_cpu_cores(
        {0: [10.0, 10.0], 1: [50.0, 50.0], 2: [90.0, 90.0]}
    ) == {"min": 10.0, "median": 50.0, "max": 90.0}, (
        "aggregate_cpu_cores: 3 steady cores -> min/median/max across cores"
    )
    assert server.aggregate_cpu_cores({0: [20.0, None], 1: [None, 60.0]}) == {
        "min": 20.0,
        "median": 40.0,
        "max": 60.0,
    }, "aggregate_cpu_cores: drops None samples within a core's burst"
    assert server.aggregate_cpu_cores({0: [None], 1: []}) is None, (
        "aggregate_cpu_cores: no finite samples -> None (key omitted)"
    )

    # missing_core_metrics: a key present but all-null still counts as missing,
    # since a Glances brownout writes {"avg": None, ...} rather than omitting it.
    assert (
        server.missing_core_metrics({"cpu": {"avg": 5.0}, "memory": {"percent": 40.0}})
        == []
    ), "missing_core_metrics: full record -> nothing missing"
    assert server.missing_core_metrics({"memory": {"percent": 40.0}}) == ["cpu"], (
        "missing_core_metrics: absent cpu key -> missing"
    )
    assert server.missing_core_metrics(
        {"cpu": {"avg": None, "min": None, "max": None}, "memory": {"percent": 1.0}}
    ) == ["cpu"], (
        "missing_core_metrics: cpu present but all-null -> missing (brownout case)"
    )
    assert server.missing_core_metrics({}) == ["cpu", "memory"], (
        "missing_core_metrics: empty record -> both missing"
    )

    # _parse_gpu_stats: real line + one with [N/A] fields (field -> None, row kept)
    stats: list[dict[str, Any]] = server._parse_gpu_stats(
        "0, 42, 1024, 24576, 50, 120.5\n1, [N/A], [N/A], 24576, [N/A], [N/A]"
    )
    assert (
        len(stats) == 2
        and stats[0]
        == {
            "index": 0,
            "util_percent": 42.0,
            "memory_used_mb": 1024.0,
            "memory_total_mb": 24576.0,
            "temperature": 50.0,
            "power_draw": 120.5,
            "temperature_memory": None,
            "throttle_sw_thermal_us": None,
            "throttle_hw_thermal_us": None,
        }
        and (stats[1]["index"] == 1)
        and (stats[1]["util_percent"] is None)
        and (stats[1]["temperature"] is None)
        and (stats[1]["power_draw"] is None)
    ), (
        f"_parse_gpu_stats: [N/A] fields -> None, row kept (index required): {describe(stats)}"
    )

    # _parse_gpu_stats: the full 9-column line (mem temp + thermal slowdown
    # counters). The P40 reports memory temp "N/A" (no brackets) and its sw
    # counter "[N/A]"; the V100 reads all of them.
    stats = server._parse_gpu_stats(
        "0, 0, 0, 24576, 22, 9.94, N/A, [N/A], 0\n"
        "1, 97, 30000, 32768, 79, 199.5, 86, 2391391956, 0"
    )
    assert (
        len(stats) == 2
        and stats[0]["temperature"] == 22.0
        and (stats[0]["temperature_memory"] is None)
        and (stats[0]["throttle_sw_thermal_us"] is None)
        and (stats[0]["throttle_hw_thermal_us"] == 0.0)
        and (stats[1]["temperature_memory"] == 86.0)
        and (stats[1]["throttle_sw_thermal_us"] == 2391391956.0)
    ), f"_parse_gpu_stats: 9 columns, P40 memory N/A -> None: {describe(stats)}"
    # ... and into the history: counters in seconds, "GPU N mem" only when readable
    recs: list[dict[str, Any]] = server.build_gpu_records(stats, [])
    assert (
        recs[0]["throttle_sw_thermal_s"] is None
        and recs[0]["throttle_hw_thermal_s"] == 0.0
        and (recs[1]["throttle_sw_thermal_s"] == 2391.4)
        and (json.loads(json.dumps(recs)) == recs)
    ), (
        f"build_gpu_records: thermal slowdown counters us -> s, None kept: {describe(recs)}"
    )
    assert server.gpu_temps(stats, []) == {
        "GPU 0": 22.0,
        "GPU 1": 79.0,
        "GPU 1 mem": 86.0,
    }, (
        f"gpu_temps: 'GPU N mem' when readable, no key for the P40's N/A: {describe(server.gpu_temps(stats, []))}"
    )

    # build_gpu_records: nvidia-smi with all metric fields [N/A] (P40-via-nvidia
    # edge) -> full record, fields None, no crash, JSON-safe.
    na_stats: list[dict[str, int | None]] = [
        {
            "index": 0,
            "util_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "temperature": None,
        }
    ]
    recs = server.build_gpu_records(na_stats, [])
    assert (
        len(recs) == 1
        and recs[0]["index"] == 0
        and (recs[0]["util_percent"] is None)
        and (recs[0]["mem_used_gb"] is None)
        and (recs[0]["mem_percent"] is None)
        and (json.loads(json.dumps(recs)) == recs)
    ), (
        f"build_gpu_records(nvidia N/A) -> record with None fields, JSON-safe: {describe(recs[0] if recs else {})}"
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
    assert (
        recs[0]["util_percent"] == 42.0
        and recs[0]["mem_used_gb"] == 1.0
        and (recs[0]["mem_total_gb"] == 24.0)
        and (recs[0]["mem_percent"] == 4.2)
    ), f"build_gpu_records(nvidia-smi) -> computed util + memory: {describe(recs[0])}"

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
    assert recs[0]["util_percent"] == 55.0 and recs[0]["mem_total_gb"] == 24.0, (
        f"build_gpu_records: nvidia util None -> Glances proc fallback: {describe(recs[0])}"
    )

    # build_gpu_records: no nvidia-smi -> Glances util only, memory None
    recs = server.build_gpu_records([], [{"gpu_id": "nvidia0", "proc": 30.0}])
    assert recs[0]["util_percent"] == 30.0 and recs[0]["mem_total_gb"] is None, (
        f"build_gpu_records(Glances only) -> util from proc, memory None: {describe(recs[0])}"
    )

    # gpu_temps: nvidia-smi primary; key emitted even when temp is None (a gap)
    assert server.gpu_temps([{"index": 0, "temperature": 23.0}], []) == {
        "GPU 0": 23.0
    } and server.gpu_temps([{"index": 0, "temperature": None}], []) == {
        "GPU 0": None
    }, "gpu_temps: value or None-but-present key"

    # aggregate_ping: loss fraction + stats; full outage -> null stats, loss 1.0
    pings: dict[str, Any] = server.aggregate_ping(
        {"8.8.8.8": [], "1.1.1.1": [10.0, 20.0], "9.9.9.9": [30.0, 30.0, 30.0]},
        {"8.8.8.8": 5, "1.1.1.1": 5, "9.9.9.9": 5},
    )
    assert pings["8.8.8.8"] == {"avg": None, "min": None, "max": None, "loss": 1.0}, (
        f"aggregate_ping: all-timeout -> null stats + loss 1.0: {describe(pings['8.8.8.8'])}"
    )
    assert pings["1.1.1.1"] == {"avg": 15.0, "min": 10.0, "max": 20.0, "loss": 0.6}, (
        f"aggregate_ping: 2/5 succeeded -> stats + loss 0.6: {describe(pings['1.1.1.1'])}"
    )
    assert pings["9.9.9.9"] == {"avg": 30.0, "min": 30.0, "max": 30.0, "loss": 0.4}, (
        f"aggregate_ping: 3/5 succeeded -> loss 0.4: {describe(pings['9.9.9.9'])}"
    )


def test_dedupe_fs_unit() -> None:
    """Unit tests for filesystem de-dup by backing device (no server needed).

    Guards the fix for systemd PrivateTmp bind mounts (/tmp, /var/tmp) that mirror
    / as duplicate 'drives'. Device names are real (/dev/dm-0 etc.) so realpath
    leaves them unchanged and the test is deterministic without symlinks.
    """
    import server

    def _mounts(entries: list[dict[str, Any]]) -> list[str]:
        return [e["mnt_point"] for e in server._dedupe_fs_by_device(entries)]

    # Phantom PrivateTmp mounts: same device + size + used as / -> only / kept,
    # even though /tmp is listed first (shortest, non-empty mount wins).
    phantom: list[dict[str, str | int]] = [
        {"device_name": "/dev/dm-0", "mnt_point": "/tmp", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/var/tmp", "size": 200, "used": 148},
    ]
    assert _mounts(phantom) == ["/"], (
        f"dedupe: /tmp + /var/tmp (same dev/size/used as /) collapse to /: {describe(_mounts(phantom))}"
    )

    # Distinct devices -> all kept, original order preserved.
    distinct: list[dict[str, str | int]] = [
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
        {"device_name": "/dev/sda2", "mnt_point": "/boot", "size": 2, "used": 1},
        {
            "device_name": "/dev/sdb1",
            "mnt_point": "/mnt/scratch",
            "size": 880,
            "used": 28,
        },
    ]
    assert _mounts(distinct) == ["/", "/boot", "/mnt/scratch"], (
        f"dedupe: distinct devices all kept, order preserved: {describe(_mounts(distinct))}"
    )

    # Same device but different `used` -> NOT the same fs, both kept (over-collapse guard).
    subvol: list[dict[str, str | int]] = [
        {"device_name": "/dev/dm-1", "mnt_point": "/data/a", "size": 500, "used": 100},
        {"device_name": "/dev/dm-1", "mnt_point": "/data/b", "size": 500, "used": 200},
    ]
    assert _mounts(subvol) == ["/data/a", "/data/b"], (
        f"dedupe: same device, different used -> both kept (no over-collapse): {describe(_mounts(subvol))}"
    )

    # Same device, `used` a few blocks apart (a busy root fs statvfs'd twice, for
    # '/' and PrivateTmp's '/tmp'): within 0.1% of size it is one fs, '/' wins.
    GB: int = 1024**3
    jitter: list[dict[str, str | int]] = [
        {
            "device_name": "/dev/nvme0n1p2",
            "mnt_point": "/tmp",
            "size": 900 * GB,
            "used": 400 * GB + 65536,
        },
        {
            "device_name": "/dev/nvme0n1p2",
            "mnt_point": "/",
            "size": 900 * GB,
            "used": 400 * GB,
        },
    ]
    assert _mounts(jitter) == ["/"], (
        f"dedupe: same device, used differs by a few KB -> one fs, shortest mount wins: {describe(_mounts(jitter))}"
    )

    # Empty device_name -> always kept (nothing to dedupe against).
    no_dev: list[dict[str, str | int]] = [
        {"device_name": "", "mnt_point": "/weird", "size": 1, "used": 0}
    ]
    assert _mounts(no_dev) == ["/weird"], (
        f"dedupe: entry with empty device_name kept as-is: {describe(_mounts(no_dev))}"
    )

    # Blank mount_point sharing a device/size/used with / -> / wins, blank dropped.
    blank: list[dict[str, str | int]] = [
        {"device_name": "/dev/dm-0", "mnt_point": "", "size": 200, "used": 148},
        {"device_name": "/dev/dm-0", "mnt_point": "/", "size": 200, "used": 148},
    ]
    assert _mounts(blank) == ["/"], (
        f"dedupe: real / wins over a blank-mount entry on the same fs: {describe(_mounts(blank))}"
    )
