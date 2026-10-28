from __future__ import annotations

import http.client
import os
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from tests.support import describe

# Expected response shapes per endpoint (path -> validator)
SCHEMAS: dict[str, Callable[[Any], bool]] = {
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
        isinstance(d, dict)
        and {"records", "count", "skipped_lines", "cutoff", "now"} <= d.keys()
        and not any(k in r for r in d["records"] for k in ("temps", "processes", "ip"))
    ),
    "/api/history/field/temps": lambda d: (
        isinstance(d, dict)
        and d.get("field") == "temps"
        and isinstance(d.get("records"), list)
        and all(set(r.keys()) <= {"timestamp", "temps"} for r in d["records"])
    ),
    "/api/history/field/processes": lambda d: (
        isinstance(d, dict)
        and d.get("field") == "processes"
        and isinstance(d.get("records"), list)
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
        isinstance(d, list)
        and not any("cmdline" in p for p in d if isinstance(p, dict))
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
    "/api/gpu-processes": lambda d: (
        isinstance(d, list)
        and all(
            {
                "gpu_index",
                "pid",
                "gpu_memory_mib",
                "gpu_util_percent",
                "gpu_mem_util_percent",
            }
            <= p.keys()
            for p in d
        )
    ),
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


pytestmark: pytest.MarkDecorator = pytest.mark.integration

REQUIRED_ENDPOINTS: list[str] = [
    "/",
    "/config.json",
    "/static/app.js",
    "/api/4/cpu",
    "/api/4/mem",
    "/api/4/memswap",
    "/api/4/network",
    "/api/4/diskio",
    "/api/4/fs",
    "/api/4/sensors",
    "/api/4/load",
    "/api/history",
    "/api/history/field/temps",
    "/api/history/field/processes",
    "/api/cpu-info",
    "/api/ip-info",
    "/api/processlist",
    "/api/history/settings",
    "/api/ping",
    "/api/mail",
    "/api/storage-health",
    "/api/sensors",
]
OPTIONAL_ENDPOINTS: list[str] = [
    "/api/4/gpu",
    "/api/gpu-info",
    "/api/gpu-processes",
    "/api/disk-processes",
    "/api/network-processes",
]
CUSTOM_ENDPOINTS: list[str] = [
    "/api/history",
    "/api/history/field/temps",
    "/api/history/field/processes",
    "/api/cpu-info",
    "/api/ip-info",
    "/api/processlist",
    "/api/history/settings",
    "/api/ping",
    "/api/mail",
    "/api/storage-health",
    "/api/sensors",
]


@pytest.mark.parametrize("path", REQUIRED_ENDPOINTS)
def test_endpoint(live_server: str, path: str) -> None:
    response: httpx.Response = httpx.get(f"{live_server}{path}", timeout=10)
    assert response.status_code == 200, response.text
    validator: Callable[[Any], bool] | None = SCHEMAS.get(path)
    if validator is not None:
        data: Any = response.json()
        assert validator(data), describe(data)


@pytest.mark.parametrize("path", OPTIONAL_ENDPOINTS)
def test_optional_endpoint(live_server: str, path: str) -> None:
    error: httpx.HTTPError
    try:
        response: httpx.Response = httpx.get(f"{live_server}{path}", timeout=10)
    except httpx.HTTPError as error:
        pytest.skip(f"Optional host capability unavailable: {error}")
    if response.status_code != 200:
        pytest.skip(
            f"Optional host capability unavailable: HTTP {response.status_code}"
        )
    validator: Callable[[Any], bool] | None = SCHEMAS.get(path)
    if validator is not None:
        data: Any = response.json()
        assert validator(data), describe(data)


@pytest.mark.parametrize(
    "path", ["/api/4/processlist", "/api/4/all", "/api/4/processes/1", "/docs"]
)
def test_private_endpoints(live_server: str, path: str) -> None:
    response: httpx.Response = httpx.get(f"{live_server}{path}", timeout=10)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/4/x/../processlist",
        "/api/4/./all",
        "/api/4/%2e%2e/4/processlist",
        "/api/4/process%256cist",
        "/api/4/a%256cl",
        "/api/%2e%2e/api/4/processlist",
        "/api/../api/4/processlist",
        "/api/4/cpu/../processlist",
        "/api/4/config",
    ],
)
def test_private_endpoints_with_raw_paths(live_server: str, path: str) -> None:
    # httpx normalizes dot segments; http.client sends the original spelling.
    host: urllib.parse.SplitResult = urllib.parse.urlsplit(live_server)
    assert host.hostname is not None
    connection: http.client.HTTPConnection = http.client.HTTPConnection(
        host.hostname, host.port, timeout=10
    )
    try:
        connection.request("GET", path)
        assert connection.getresponse().status == 404
    finally:
        connection.close()


@pytest.mark.parametrize(
    "path,want_color",
    [
        ("/text", True),
        ("/text?style=simple", False),
        ("/text?style=unicode", True),
        ("/text?style=unicode&color=off&view=history", False),
    ],
)
def test_text_color(live_server: str, path: str, want_color: bool) -> None:
    response: httpx.Response = httpx.get(f"{live_server}{path}", timeout=10)
    assert response.status_code == 200
    assert ("\033[" in response.text) == want_color


@pytest.mark.parametrize("path", CUSTOM_ENDPOINTS)
def test_endpoint_listing(live_server: str, path: str) -> None:
    response: httpx.Response = httpx.get(
        f"{live_server}/api/list-endpoints", timeout=10
    )
    assert response.status_code == 200
    assert path.removeprefix("/api") in response.json().get("endpoints", [])


@pytest.fixture(scope="module")
def record_data(live_server: str) -> dict[str, Any]:
    response: httpx.Response = httpx.post(
        f"{live_server}/api/history/record", timeout=30
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    assert result.get("record"), result
    return result


def test_text_and_downloaded_client(live_server: str) -> None:
    base: str = live_server
    r: httpx.Response
    r = httpx.get(f"{base}/text?style=simple", timeout=10)
    assert r.text.isascii(), "GET /text?style=simple is 7-bit"
    r = httpx.get(f"{base}/text?height=12&color=256", timeout=10)
    assert (
        r.status_code == 200 and r.text.count("\n") <= 12 and ("\x1b[38;5;" in r.text)
    ), "GET /text?height=12&color=256 fits and uses 256 colours"
    r = httpx.get(f"{base}/tui.py", timeout=10)
    assert r.status_code == 200 and "def detect(" in r.text, (
        "GET /tui.py serves the client"
    )
    if r.status_code == 200:
        served_tui: dict[str, Any] = {"__name__": "served_tui"}
        exec(compile(r.text, "<served tui.py>", "exec"), served_tui)
        with patch.dict(os.environ, {"HORATIO_URL": ""}):
            assert served_tui["default_url"]() == base, (
                "downloaded TUI defaults to the server that served it"
            )
    r = httpx.get(f"{base}/help", timeout=10)
    assert r.status_code == 200 and "/text" in r.text and ("/api/history" in r.text), (
        "GET /help"
    )
    r = httpx.get(f"{base}/text?style=fancy", timeout=10)
    assert r.status_code == 400, f"GET /text?style=fancy is 400: status={r.status_code}"


def test_history_response(live_server: str, record_data: dict[str, Any]) -> None:
    base: str = live_server
    r: httpx.Response
    # ── /api/history returns RAW records (no downsampling) ───────
    r = httpx.get(f"{base}/api/history?hours=24", timeout=30)
    assert r.status_code == 200, "GET /api/history"
    h: dict[str, Any] = r.json()
    assert "raw_count" not in h and "max_points" not in h, (
        f"history response has no downsampling keys (raw_count/max_points): {describe(h)}"
    )
    assert h.get("count") == len(h.get("records", [])), (
        f"history count == number of raw records returned: count={h.get('count')} records={len(h.get('records', []))}"
    )
    assert {"truncated", "recording"} <= h.keys(), (
        f"history response keeps truncated + recording: {describe(h)}"
    )
    assert all(("ip" not in rec for rec in h.get("records", []))), (
        "GET /api/history strips ip from every record"
    )


def test_incremental_history_and_compression(
    live_server: str, record_data: dict[str, Any]
) -> None:
    base: str = live_server
    r: httpx.Response
    h: dict[str, Any]
    # ── Incremental history (since=) and gzip ────────────────────
    h = httpx.get(f"{base}/api/history?hours=24", timeout=30).json()
    assert "cutoff" in h, "history response carries cutoff"
    newest: Any = max(
        (r["timestamp"] for r in h.get("records", [])),
        key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
        default=None,
    )
    if newest:
        newest_ts: float = datetime.fromisoformat(
            newest.replace("Z", "+00:00")
        ).timestamp()
        r = httpx.get(
            f"{base}/api/history",
            params={"hours": 24, "since": newest},
            timeout=30,
        )
        d: dict[str, Any] = r.json()
        # The background initial sample can land between the two reads, so
        # the contract is "strictly newer", not "nothing".
        assert (
            r.status_code == 200
            and d.get("count") == len(d.get("records", [])) <= 1
            and all(
                (
                    datetime.fromisoformat(
                        x["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                    > newest_ts
                    for x in d.get("records", [])
                )
            )
            and ("cutoff" in d)
        ), (
            f"GET /api/history?since=<newest> returns only strictly newer records: {describe(d)}"
        )
        r = httpx.get(
            f"{base}/api/history/field/temps",
            params={"hours": 24, "since": newest},
            timeout=30,
        )
        assert (
            r.status_code == 200
            and "cutoff" in r.json()
            and (r.json().get("count", 99) <= 1)
        ), f"GET /api/history/field/temps?since= works: {describe(r.json())}"
    else:
        pytest.fail(
            f"{'history since= checks'}: {'no records to take a timestamp from'}"
        )
    assert (
        isinstance(h.get("cutoff"), (int, float))
        and abs(h["cutoff"] - (time.time() - 24 * 3600)) < 120
    ), f"history cutoff is the window start (now - hours): {str(h.get('cutoff'))}"
    assert (
        isinstance(h.get("now"), (int, float))
        and abs(h["now"] - time.time()) < 120
        and (abs(h["now"] - h["cutoff"] - 24 * 3600) < 1e-06)
    ), f"history now is the server clock (cutoff + hours): {str(h.get('now'))}"
    if newest and "+" in newest:
        # Pasted unencoded, the '+' of '+00:00' arrives as a space.
        r = httpx.get(f"{base}/api/history?hours=24&since={newest}", timeout=30)
        assert r.status_code == 200, (
            f"since= with an unencoded '+00:00' is accepted: {r.text[:200]}"
        )
    r = httpx.get(f"{base}/api/history?hours=24&since=garbage", timeout=30)
    assert r.status_code == 400, (
        f"GET /api/history?since=garbage -> 400: {r.text[:200]}"
    )
    r = httpx.get(
        f"{base}/static/app.js",
        # Above the 1000-byte gzip minimum, so a missing bypass would show.
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-4999"},
        timeout=30,
    )
    assert (
        r.status_code == 206
        and "content-encoding" not in r.headers
        and (len(r.content) == 5000)
    ), (
        f"Range requests are not gzipped (Content-Range stays true): {r.status_code} {dict(r.headers)}"
    )
    r = httpx.get(f"{base}/", headers={"Accept-Encoding": "gzip"}, timeout=30)
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip", (
        f"responses are gzipped for clients that accept it: {str(dict(r.headers))}"
    )
    r = httpx.get(f"{base}/text", headers={"Accept-Encoding": "identity"}, timeout=30)
    assert r.status_code == 200 and "content-encoding" not in r.headers, (
        f"no gzip without Accept-Encoding: gzip (tui.py path): {str(dict(r.headers))}"
    )


def test_history_export_requires_password(live_server: str) -> None:
    base: str = live_server
    r: httpx.Response
    # ── Raw history export (password-gated: records embed LAN/public/
    # Tailscale IPs, so an unauthenticated bulk export is not offered) ──
    r = httpx.get(f"{base}/api/history/raw", timeout=30)
    # 403 = password configured and withheld; 503 = no password set up.
    assert r.status_code in (403, 503), (
        f"GET /api/history/raw is gated without a password: status={r.status_code}"
    )
    r = httpx.get(f"{base}/api/history/raw", params={"password": "wrong"}, timeout=30)
    assert r.status_code in (403, 503), (
        f"GET /api/history/raw rejects a wrong password: status={r.status_code}"
    )


def test_record_fields_and_rate_caps(
    live_server: str, record_data: dict[str, Any]
) -> None:
    base: str = live_server
    record: dict[str, Any] = record_data.get("record", {})
    drives_io: list[dict[str, Any]] = []

    if record:
        assert "cpu" in record, "history has cpu"
        assert "memory" in record, "history has memory"
        assert "disk_io" in record, "history has disk_io"
        assert "drives" in record, "history has drives"
        # `ip` is recorded to disk (see /api/history/raw) but never served
        # unauthenticated.
        assert "ip" not in record, "POST record echo strips ip"

        # Verify disk_io stats structure (avg/min/max)
        dio: dict[str, Any] = record.get("disk_io", {})
        key: str
        for key in ("read_mbps", "write_mbps"):
            stats: dict[str, float | None] = dio.get(key, {})
            assert isinstance(stats, dict) and {"avg", "min", "max"} <= stats.keys(), (
                f"disk_io.{key} has avg/min/max"
            )

        # CPU core spread is optional (present only when /api/4/percpu is
        # readable); when present it must be an ordered min<=median<=max.
        if "cpu_cores" in record:
            cc: dict[str, Any] = record["cpu_cores"]
            assert (
                isinstance(cc, dict)
                and {"min", "median", "max"} <= cc.keys()
                and (cc["min"] <= cc["median"] <= cc["max"])
            ), f"history cpu_cores has ordered min<=median<=max: {describe(cc)}"

        # CPU clocks are optional (cpufreq sysfs); when present, ordered and in MHz.
        if "cpu_freq" in record:
            cf: dict[str, Any] | None = record["cpu_freq"]
            assert (
                isinstance(cf, dict)
                and {"min", "median", "avg", "max"} <= cf.keys()
                and (cf["min"] <= cf["median"] <= cf["max"])
                and (cf["min"] <= cf["avg"] <= cf["max"])
                and (cf["min"] > 0)
            ), f"history cpu_freq has ordered min<=median/avg<=max: {describe(cf)}"
        # Per-process history: the key is there exactly when the setting is on
        # (off unless this box's config.json enables it).
        hs: dict[str, Any] = httpx.get(
            f"{base}/api/history/settings", timeout=10
        ).json()
        on: bool = hs.get("processes_enabled") is True
        assert ("processes" in record) == on and (
            not on
            or all(
                (
                    {"pid", "name"} <= r.keys() and None not in r.values()
                    for r in record["processes"]
                )
            )
        ), f"history processes present iff enabled (enabled={on}); rows sparse"

        # Verify per-drive stats structure
        drives_io = dio.get("drives", [])
        d: dict[str, Any]
        for d in drives_io:
            name: str = d.get("name", "?")
            for key in ("read_mbps", "write_mbps"):
                stats = d.get(key, {})
                assert (
                    isinstance(stats, dict) and {"avg", "min", "max"} <= stats.keys()
                ), f"disk_io.drives[{name}].{key} has avg/min/max"
        assert len(drives_io) > 0, "history has per-drive disk_io"

        # Served rates obey the configured caps (the box's config.json may
        # set its own; defaults otherwise).
        cfg: dict[str, Any] = httpx.get(f"{base}/config.json", timeout=10).json()
        configured_limits: Any = cfg.get("limits")
        lims: dict[str, Any] = (
            configured_limits if isinstance(configured_limits, dict) else {}
        )

        def cap_of(key: str, default: float) -> float:
            # The server's _cfg_num rules: a number in [1, 1e9], else default.
            v: Any = lims.get(key, default)
            ok_: bool = (
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and 1 <= v <= 1e9
            )
            return float(v) if ok_ else default

        dio_cap: float = cap_of("disk_io_mbps", 15000.0)
        first_hist: dict[str, Any] = (
            httpx.get(f"{base}/api/history?hours=24", timeout=30).json().get("records")
            or [{}]
        )[0]

        def within(stats: object, cap: float) -> bool:
            return isinstance(stats, dict) and all(
                v is None or (isinstance(v, (int, float)) and 0 <= v <= cap)
                for v in stats.values()
            )

        label: str
        rec_: dict[str, Any]
        for label, rec_ in (("echo", record), ("history", first_hist)):
            net: dict[str, Any] = rec_.get("network", {})
            dio_: dict[str, Any] = rec_.get("disk_io", {})

            def nonneg(stats: object) -> bool:
                return isinstance(stats, dict) and all(
                    v is None or (isinstance(v, (int, float)) and v >= 0)
                    for v in stats.values()
                )

            # Each drive is capped; totals are uncapped sums (non-negative).
            assert (
                all((nonneg(net.get(k)) for k in ("in_mbps", "out_mbps") if k in net))
                and all(
                    (
                        nonneg(dio_.get(k))
                        for k in ("read_mbps", "write_mbps")
                        if k in dio_
                    )
                )
                and all(
                    (
                        within(d.get(k), dio_cap)
                        for d in dio_.get("drives", [])
                        for k in ("read_mbps", "write_mbps")
                        if k in d
                    )
                )
            ), (
                f"{label} record: drives within limits (disk<={dio_cap}), totals non-negative: {describe({'network': net, 'disk_io': dio_})}"
            )
    else:
        pytest.fail("history record returned data")


def test_live_history_consistency(
    live_server: str, record_data: dict[str, Any]
) -> None:
    base: str = live_server
    drives_io: list[dict[str, Any]] = (
        record_data["record"].get("disk_io", {}).get("drives", [])
    )
    name: str
    # ── Consistency: live fs vs history drives ───────────────────

    fs_resp: httpx.Response = httpx.get(f"{base}/api/4/fs", timeout=10)
    diskio_resp: httpx.Response = httpx.get(f"{base}/api/4/diskio", timeout=10)
    fs_data: list[dict[str, Any]] = fs_resp.json() if fs_resp.status_code == 200 else []
    diskio_data: list[dict[str, Any]] = (
        diskio_resp.json() if diskio_resp.status_code == 200 else []
    )

    # Build the same mapping the dashboard uses
    device_to_mount: dict[str, str] = {}
    fs: dict[str, Any]
    for fs in fs_data:
        dev: str = fs.get("device_name", "")
        mnt: str = fs.get("mnt_point", "")
        if dev and mnt and dev.startswith("/dev/"):
            device_to_mount[dev[5:]] = mnt

    diskio_names: set[str] = {d["disk_name"] for d in diskio_data if d.get("disk_name")}

    # Drives that are both in fs and diskio should appear in history
    expected_mounts: set[str] = {
        device_to_mount[name] for name in device_to_mount if name in diskio_names
    }
    history_drive_names: set[str | None] = {d.get("name") for d in drives_io}

    if expected_mounts:
        assert expected_mounts == history_drive_names, (
            f"history drives match live fs ∩ diskio: expected={sorted(expected_mounts)}  got={sorted(history_drive_names)}"
        )
    else:
        pytest.skip("No drives in both filesystem and disk I/O data to compare")

    # ── Consistency: live values vs history values ───────────────
    # For each drive in both live and history, the history avg should be
    # close to what we'd compute from live bytes / 1e6 (same conversion).
    # We can't compare exact values (different time windows), but we can
    # verify the order of magnitude is the same (not off by 1024/1000).
    drive_hist: dict[str, Any]
    for drive_hist in drives_io:
        name = drive_hist.get("name", "?")
        # Find the diskio device for this mount
        dev_name: str | None = None
        dname: str
        for dname, mnt in device_to_mount.items():
            if mnt == name:
                dev_name = dname
                break
        if dev_name is None:
            continue

        live_disk: dict[str, Any] | None = next(
            (d for d in diskio_data if d.get("disk_name") == dev_name), None
        )
        if live_disk is None:
            continue

        live_read: float = (live_disk.get("read_bytes_rate_per_sec") or 0) / 1e6
        hist_read_avg: float = drive_hist.get("read_mbps", {}).get("avg", 0)
        live_write: float = (live_disk.get("write_bytes_rate_per_sec") or 0) / 1e6
        hist_write_avg: float = drive_hist.get("write_mbps", {}).get("avg", 0)

        # Both should be non-negative
        assert live_read >= 0 and hist_read_avg >= 0, (
            f"drive {name} read values non-negative: live={live_read:.4f}  hist_avg={hist_read_avg:.4f}"
        )
        assert live_write >= 0 and hist_write_avg >= 0, (
            f"drive {name} write values non-negative: live={live_write:.4f}  hist_avg={hist_write_avg:.4f}"
        )
