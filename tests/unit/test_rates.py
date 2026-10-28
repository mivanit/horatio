from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from tests.support import describe, history_line


def test_rate_clamp_unit() -> None:
    """Rate caps: _cfg_num, _clamp_rate, clamp_record_rates, and the read path."""
    import server

    # _cfg_num: ints and floats in range pass (as float); bool, strings, 0 and
    # non-finite fall back to the default with a warning.
    def num(v: object) -> float:
        return server._cfg_num({"k": v}, "k", 7.0, 1, 1e9, "t")

    assert num(1250) == 1250.0 and isinstance(num(1250), float), "_cfg_num int -> float"
    assert num(1250.5) == 1250.5, "_cfg_num float passes"
    assert num(True) == 7.0, "_cfg_num rejects bool"
    assert num("x") == 7.0, "_cfg_num rejects str"
    assert num(0) == 7.0, "_cfg_num rejects below lo"
    assert num(float("inf")) == 7.0 and num(float("nan")) == 7.0, (
        "_cfg_num rejects inf/nan"
    )
    assert server._cfg_num({}, "k", 7.0, 1, 1000000000.0, "t") == 7.0, (
        "_cfg_num missing -> default"
    )

    # _read_limits: the "limits" section, not thresholds; defaults otherwise.
    assert server._read_limits(
        {"limits": {"network_mbps": 100, "disk_io_mbps": 2000.5}}
    ) == (100.0, 2000.5), "_read_limits reads config.json limits"
    assert server._read_limits(
        {"thresholds": {"network_mbps": 3, "disk_io_mbps": 50}}
    ) == (1250.0, 15000.0), "_read_limits ignores thresholds (same key names)"
    assert server._read_limits({}) == (1250.0, 15000.0), (
        "_read_limits defaults without limits"
    )
    assert (
        server._read_limits({"limits": "fast"}) == (1250.0, 15000.0)
        and server._read_limits({"limits": {"network_mbps": 0}})[0] == 1250.0
    ), "_read_limits: non-object section or bad value -> defaults"

    assert server._clamp_rate(-3.0, 10.0) == 0.0, "_clamp_rate floors at 0"
    assert server._clamp_rate(1000000000.0, 10.0) == 10.0, "_clamp_rate caps"
    assert server._clamp_rate(4.5, 10.0) == 4.5, "_clamp_rate passes sane values"

    rc: server._RateCaps = server._RateCaps()
    vals: list[float] = [
        rc.sample(v * 1e6, 10.0, ("network.in_mbps", "eth0"))
        for v in (4.0, 50.0, -1.0, 99.0)
    ]
    assert vals == [4.0, 10.0, 0.0, 10.0], (
        f"_RateCaps.sample: bytes/s -> MB/s, clamps without dropping: {str(vals)}"
    )
    odd: list[float] = [
        rc.sample(v, 10.0, ("network.in_mbps", "eth0"))
        for v in (float("inf"), float("nan"), "x", None, True)
    ]
    assert odd == [0.0] * 5, (
        f"_RateCaps.sample: missing/non-finite/non-numeric -> 0: {str(odd)}"
    )
    assert rc.seen == {("network.in_mbps", "eth0"): 4} and rc.hits == {
        ("network.in_mbps", "eth0"): [50.0, 99.0]
    }, (
        f"_RateCaps counts only real samples and keeps over-cap raw values: seen={rc.seen} hits={rc.hits}"
    )

    # _parse_since: encoded, unencoded '+', Z, naive (= UTC), space separator, junk.
    ref: float = datetime(
        2026, 10, 23, 12, 0, 0, 500000, tzinfo=timezone.utc
    ).timestamp()
    text: str
    for text in (
        "2026-10-23T12:00:00.5+00:00",
        "2026-10-23T12:00:00.5 00:00",
        "2026-10-23T12:00:00.5Z",
        "2026-10-23T12:00:00.5",
        "2026-10-23 12:00:00.5",
    ):
        assert server._parse_since(text) == ref, f"_parse_since({text!r})"
    assert server._parse_since(None) is None and server._parse_since("") is None, (
        "_parse_since absent -> None"
    )
    bad: float | None | server.Response = server._parse_since("garbage")
    assert getattr(bad, "status_code", None) == 400, "_parse_since junk -> 400"

    # Caps pinned here so the expectations don't depend on this box's config.json.
    orig_caps: tuple[float, float] = (server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS)
    server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = 1250.0, 15000.0
    try:
        _rate_clamp_record_checks()
        _collect_sample_cap_checks()
    finally:
        server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = orig_caps


def _rate_clamp_record_checks() -> None:
    """clamp_record_rates and the read path, at the default caps."""
    import server

    net_cap: float
    dio_cap: float
    net_cap, dio_cap = 1250.0, 15000.0
    spike: dict[str, float] = {"avg": 1e9, "min": -5.0, "max": 1e12}
    clamped: dict[str, float] = {"avg": net_cap, "min": 0.0, "max": net_cap}
    dclamped: dict[str, float] = {"avg": dio_cap, "min": 0.0, "max": dio_cap}

    # A record from before per-device capping (no rate_caps): only totals exist,
    # so the totals are clamped, both directions.
    legacy: dict[str, Any] = {
        "network": {"in_mbps": dict(spike), "out_mbps": dict(spike)},
        "disk_io": {
            "read_mbps": dict(spike),
            "write_mbps": dict(spike),
            "drives": [
                {"name": "/", "read_mbps": dict(spike), "write_mbps": dict(spike)},
                {
                    "name": "/data",
                    "read_mbps": {"avg": None, "min": None, "max": None},
                    "write_mbps": "bogus",
                },
                "not a dict",
            ],
        },
        "cpu": {"avg": 1e9},
    }
    server.clamp_record_rates(legacy)
    assert (
        legacy["network"]["in_mbps"] == clamped
        and legacy["network"]["out_mbps"] == clamped
    ), (
        f"legacy record: network in and out totals clamped, negatives floored: {describe(legacy['network'])}"
    )
    assert (
        legacy["disk_io"]["read_mbps"] == dclamped
        and legacy["disk_io"]["write_mbps"] == dclamped
    ), "legacy record: disk read and write totals clamped"
    drives: list[Any] = legacy["disk_io"]["drives"]
    assert (
        drives[0]["read_mbps"] == dclamped
        and drives[0]["write_mbps"] == dclamped
        and (drives[1]["read_mbps"] == {"avg": None, "min": None, "max": None})
        and (drives[1]["write_mbps"] == "bogus")
        and (drives[2] == "not a dict")
    ), "per-drive read and write clamped; None stats and odd entries left alone"
    assert legacy["cpu"] == {"avg": 1000000000.0}, "other metrics untouched"

    # A per-device-capped record (rate_caps): totals are sums that may rightly
    # exceed one cap, so they are left alone; drives are still clamped.
    big: dict[str, float] = {"avg": 3000.0, "min": 2000.0, "max": 4000.0}
    new: dict[str, Any] = {
        "rate_caps": {"network_mbps": net_cap, "disk_io_mbps": dio_cap},
        "network": {"in_mbps": dict(big), "out_mbps": dict(big)},
        "disk_io": {
            "read_mbps": {"avg": 20000.0, "min": 0.0, "max": 25000.0},
            "write_mbps": {"avg": 1.0, "min": 1.0, "max": 1.0},
            "drives": [
                {"name": "/", "read_mbps": dict(spike), "write_mbps": dict(spike)}
            ],
        },
    }
    server.clamp_record_rates(new)
    assert (
        new["network"]["in_mbps"] == big
        and new["network"]["out_mbps"] == big
        and (new["disk_io"]["read_mbps"]["max"] == 25000.0)
    ), f"per-device-capped record: totals over one cap are left alone: {describe(new)}"
    assert new["disk_io"]["drives"][0]["read_mbps"] == spike, (
        "per-device-capped record: served as recorded (drives too)"
    )
    server.clamp_record_rates({"network": None, "disk_io": 5})
    server.clamp_record_rates({})

    # The read path serves clamped values for legacy records and never rewrites.
    orig_history_file: Path = server.HISTORY_FILE
    try:
        tmp: str
        with tempfile.TemporaryDirectory() as tmp:
            hist: Path = Path(tmp) / "history.jsonl"
            server.HISTORY_FILE = hist
            hist.write_text(
                history_line(
                    1,
                    network={
                        "in_mbps": {"avg": 1e9, "min": 1e9, "max": 1e9},
                        "out_mbps": {"avg": 0.5, "min": 0.1, "max": 0.9},
                    },
                    disk_io={
                        "read_mbps": {"avg": 1e9, "min": 0.0, "max": 1e9},
                        "write_mbps": {"avg": 1.0, "min": 1.0, "max": 1.0},
                    },
                )
            )
            result: tuple[list[dict[str, Any]], int, bool, float] | str = (
                server._read_history(24)
            )
            assert not isinstance(result, str), result
            served: dict[str, Any] = result[0][0]
            assert (
                served["network"]["in_mbps"]["avg"] == net_cap
                and served["network"]["out_mbps"]["max"] == 0.9
                and (served["disk_io"]["read_mbps"]["max"] == dio_cap)
                and (served["disk_io"]["write_mbps"]["avg"] == 1.0)
            ), (
                f"_read_history serves clamped legacy rates, sane values untouched: {describe(served)}"
            )
            raw: dict[str, Any] = json.loads(hist.read_text())
            assert raw["network"]["in_mbps"]["avg"] == 1000000000.0, (
                "reading never rewrites the file"
            )
    finally:
        server.HISTORY_FILE = orig_history_file


def _collect_sample_cap_checks() -> None:
    """collect_history_sample against a fake Glances: each interface and each
    drive is capped on its own (a spike counts as the cap, never dropped),
    totals are uncapped sums, bad rates are ignored without losing the record,
    and the record lists what hit a cap; the log line is for recorded samples
    only. Caps are lowered so the numbers are easy to read."""
    import server

    server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = 100.0, 200.0
    M: float = 1e6  # MB/s -> bytes/s
    calls: dict[str, int] = {"network": 0, "diskio": 0}
    # Per sample (3 samples), MB/s. eth0 spikes in (sample 2) and out (sample 3);
    # docker0 carries steady traffic that also crossed eth0, so the in total of
    # sample 2 (100 + 80) rightly exceeds the 100 cap and is not capped; wlan0
    # reports Infinity and a string, which are ignored.
    eth0_in: list[float]
    eth0_out: list[float]
    eth0_in, eth0_out = [1, 5e6, 2], [1, 1, 3e6]
    docker_in: list[int] = [80, 80, 80]
    wlan0_in: list[float | str] = [float("inf"), "x", 1]
    sda1_read: list[float]
    sda1_write: list[float]
    sda1_read, sda1_write = [1, 9e8, 1], [0, 0, 7e8]
    sdb_read: list[int] = [150, 150, 150]  # mounted: total read in sample 2 = 200 + 150

    def handler(request: httpx.Request) -> httpx.Response:
        path: str = request.url.path
        if path == "/api/4/network":
            i: int = calls["network"] % 3
            calls["network"] += 1
            wlan_rate: float | str = wlan0_in[i]
            body: str = json.dumps(
                [
                    {
                        "interface_name": "eth0",
                        "bytes_recv_rate_per_sec": eth0_in[i] * M,
                        "bytes_sent_rate_per_sec": eth0_out[i] * M,
                    },
                    {
                        "interface_name": "docker0",
                        "bytes_recv_rate_per_sec": docker_in[i] * M,
                        "bytes_sent_rate_per_sec": 0,
                    },
                    {
                        "interface_name": "wlan0",
                        "bytes_recv_rate_per_sec": wlan_rate * M
                        if isinstance(wlan_rate, (int, float))
                        else wlan_rate,
                        "bytes_sent_rate_per_sec": None,
                    },
                    {
                        "interface_name": "lo",
                        "bytes_recv_rate_per_sec": 9e15,
                        "bytes_sent_rate_per_sec": 9e15,
                    },
                ]
            )  # json.dumps writes Infinity, as Glances' stdlib json would
            return httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        if path == "/api/4/diskio":
            i = calls["diskio"] % 3
            calls["diskio"] += 1
            rows: list[dict[str, str | float]] = [
                {
                    "disk_name": "sda1",
                    "read_bytes_rate_per_sec": sda1_read[i] * M,
                    "write_bytes_rate_per_sec": sda1_write[i] * M,
                },
                {
                    "disk_name": "sdb",
                    "read_bytes_rate_per_sec": sdb_read[i] * M,
                    "write_bytes_rate_per_sec": 0,
                },
                {
                    "disk_name": "md0",
                    "read_bytes_rate_per_sec": 9e15,
                    "write_bytes_rate_per_sec": 0,
                },
            ]
            if i == 1:  # appears for one sample only
                rows.append(
                    {
                        "disk_name": "sdc",
                        "read_bytes_rate_per_sec": 5e14,
                        "write_bytes_rate_per_sec": 0,
                    }
                )
            return httpx.Response(200, json=rows)
        if path == "/api/4/fs":
            return httpx.Response(
                200,
                json=[
                    {
                        "device_name": "/dev/sda1",
                        "mnt_point": "/",
                        "size": 100,
                        "used": 50,
                        "percent": 50,
                    },
                    {
                        "device_name": "/dev/sdb",
                        "mnt_point": "/data",
                        "size": 100,
                        "used": 10,
                        "percent": 10,
                    },
                ],
            )
        if path == "/api/4/cpu":
            return httpx.Response(200, json={"total": 5.0})
        return httpx.Response(404, json={})

    state_keys: tuple[str, ...] = (
        "history_sample_duration",
        "history_sample_count",
        "history_processes_enabled",
        "history_process_count",
        "ping_enabled",
    )
    orig_state: dict[str, Any] = {
        k: getattr(server.APP.state, k, None) for k in state_keys
    }

    class FakeClient(httpx.AsyncClient):
        def __init__(self: FakeClient, **k: Any) -> None:
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(**k)

    class Capture(logging.Handler):
        def __init__(self: Capture) -> None:
            super().__init__(logging.WARNING)
            self.messages: list[str] = []

        def emit(self: Capture, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    cap_log: Capture = Capture()
    server.logger.addHandler(cap_log)
    with (
        patch.object(server.httpx, "AsyncClient", FakeClient),
        patch.object(server, "get_gpu_stats", return_value=[]),
        patch.object(server, "_get_ip_info", return_value={}),
    ):
        try:
            k: str
            v: Any
            for k, v in zip(state_keys, (0, 3, False, 10, False)):
                setattr(server.APP.state, k, v)
            rec: dict[str, Any] = asyncio.run(
                server.collect_history_sample("http://glances.invalid")
            )
            recorded_log: list[str] = [
                m for m in cap_log.messages if m.startswith("Rate cap hit")
            ]
            cap_log.messages.clear()
            calls.update(network=1, diskio=1)  # the live sample is the spiky one
            live: dict[str, Any] = asyncio.run(
                server.collect_history_sample("http://glances.invalid", live=True)
            )
            live_log: list[str] = [
                m for m in cap_log.messages if m.startswith("Rate cap hit")
            ]
        finally:
            server.logger.removeHandler(cap_log)
            for k, v in orig_state.items():
                setattr(server.APP.state, k, v)

    # in totals: 1+80+0, 100+80+0, 2+80+1  (eth0 capped; wlan0 bad values -> 0)
    assert rec.get("network", {}).get("in_mbps") == {
        "avg": round(344 / 3, 2),
        "min": 81.0,
        "max": 180.0,
    }, (
        f"network: each interface capped, total is their uncapped sum, bad rates ignored: {describe(rec.get('network'))}"
    )
    assert rec.get("network", {}).get("out_mbps", {}).get("max") == 100.0, (
        f"network out capped per interface too: {describe(rec.get('network', {}).get('out_mbps'))}"
    )
    drives: dict[str, dict[str, Any]] = {
        d["name"]: d for d in rec.get("disk_io", {}).get("drives", [])
    }
    assert (
        drives.get("/", {}).get("read_mbps", {}).get("max") == 200.0
        and drives.get("/", {}).get("write_mbps", {}).get("max") == 200.0
        and (drives.get("/data", {}).get("read_mbps", {}).get("max") == 150.0)
    ), f"per-drive read and write capped: {describe(drives)}"
    assert rec.get("disk_io", {}).get("read_mbps", {}).get("max") == 350.0, (
        f"disk total is the uncapped sum of capped drives (200 + 150): {describe(rec.get('disk_io', {}).get('read_mbps'))}"
    )
    assert rec.get("rate_caps") == {"network_mbps": 100.0, "disk_io_mbps": 200.0}, (
        f"record notes the caps it was taken under (rate_caps): {describe(rec.get('rate_caps'))}"
    )
    expected: list[dict[str, str | float]] = [
        {
            "metric": "disk_io.read_mbps",
            "device": "md0",
            "samples": 3,
            "of": 3,
            "raw_max": 9e9,
            "cap": 200.0,
        },
        {
            "metric": "disk_io.read_mbps",
            "device": "sda1",
            "drive": "/",
            "samples": 1,
            "of": 3,
            "raw_max": 9e8,
            "cap": 200.0,
        },
        {
            "metric": "disk_io.read_mbps",
            "device": "sdc",
            "samples": 1,
            "of": 1,
            "raw_max": 5e8,
            "cap": 200.0,
        },
        {
            "metric": "disk_io.write_mbps",
            "device": "sda1",
            "drive": "/",
            "samples": 1,
            "of": 3,
            "raw_max": 7e8,
            "cap": 200.0,
        },
        {
            "metric": "network.in_mbps",
            "interface": "eth0",
            "samples": 1,
            "of": 3,
            "raw_max": 5e6,
            "cap": 100.0,
        },
        {
            "metric": "network.out_mbps",
            "interface": "eth0",
            "samples": 1,
            "of": 3,
            "raw_max": 3e6,
            "cap": 100.0,
        },
    ]
    assert rec.get("rate_capped") == expected, (
        f"rate_capped: one entry per interface/device, its own sample count, raw max: {json.dumps(rec.get('rate_capped'))}"
    )
    assert len(recorded_log) == 1, (
        f"one 'Rate cap hit' log line per recorded sample: {str(recorded_log)}"
    )
    assert bool(live.get("rate_capped")) and (not live_log), (
        f"live (/text) samples note caps but never log: capped={live.get('rate_capped')} log={live_log}"
    )
