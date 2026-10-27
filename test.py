"""Integration test for the dashboard server.

Starts the server (which auto-starts Glances), pings all API endpoints,
triggers a history record, and checks consistency between live and
historical data.

Usage: uv run python test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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
        isinstance(d, dict) and {"records", "count", "skipped_lines", "cutoff", "now"} <= d.keys()
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
    "/api/gpu-processes": lambda d: isinstance(d, list) and all(
        {"gpu_index", "pid", "gpu_memory_mib", "gpu_util_percent", "gpu_mem_util_percent"} <= p.keys()
        for p in d
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
            # The dashboard serves static files before its Glances child is
            # ready. Wait for both so a slow cold start does not fail the first
            # several endpoint checks with transient 502 responses.
            if r.status_code == 200 and httpx.get(f"{base}/api/4/cpu", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _history_line(age_hours: float, pad: int = 0, **fields: object) -> str:
    """Build a history JSONL line with a timestamp age_hours in the past; extra
    keyword arguments become record fields (e.g. a network block)."""
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    record: dict[str, object] = {"timestamp": ts.isoformat(), "cpu": {"avg": 1.0}}
    if pad:
        record["pad"] = "x" * pad
    record.update(fields)
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
                hours: float, initial: int = 1024 * 1024, since: float | None = None
            ) -> tuple[list[dict], int]:
                result = server._read_history(hours, initial_read_size=initial, since=since)
                if isinstance(result, str):
                    raise AssertionError(f"_read_history failed: {result}")
                records, skipped, _truncated, _cutoff = result
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

            # 4. An unterminated last line is a record still being appended: left
            #    for the next read, not counted as corrupt.
            hist.write_text(
                _history_line(30) + _history_line(2) + '{"timestamp": "2026-'
            )
            records, skipped = read(24)
            check(
                len(records) == 1 and skipped == 0,
                "unterminated last line left for the next read, not counted",
                describe(records),
            )

            # 5. Malformed lines mid-file are skipped and counted, neighbors kept
            #    (including valid JSON whose timestamp is not a string)
            hist.write_text(_history_line(3) + "not json\n" + "42\n" + '{"timestamp": null}\n'
                            + '{"timestamp": 5}\n' + _history_line(2))
            records, skipped = read(24)
            check(
                len(records) == 2 and skipped == 4,
                "malformed mid-file lines (incl. non-string timestamps) skipped and counted",
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
            records, skipped, truncated, _ = server._read_history(
                24, initial_read_size=4096, max_bytes=64 * 1024
            )
            check(
                truncated and len(records) > 0,
                "byte cap trips truncated=True with partial recent records",
                f"{len(records)} records, truncated={truncated}",
            )
            # Within-cap read of the same file is not truncated.
            _, _, truncated_full, _ = server._read_history(
                24, initial_read_size=1024 * 1024
            )
            check(not truncated_full, "within-cap read is not truncated")

            # 10. cutoff: the window start as epoch seconds, also for a missing file.
            hist.unlink()
            missing = server._read_history(24)
            now = datetime.now(timezone.utc).timestamp()
            check(
                isinstance(missing, tuple)
                and missing[:3] == ([], 0, False)
                and abs(missing[3] - (now - 24 * 3600)) < 2,
                "missing file -> ([], 0, False, cutoff) with cutoff = now - hours",
                describe(missing),
            )

            # 11. since: only records strictly newer than it, covered by the first
            #     window even when the range would need several doublings.
            hist.write_text("".join(_history_line(20 - i, pad=100) for i in range(20)))
            full, _ = read(24, initial=256)

            def ts_of(r: dict) -> float:
                return datetime.fromisoformat(r["timestamp"]).timestamp()

            second_newest = ts_of(full[-2])
            newer, skipped = read(24, initial=256, since=second_newest)
            check(
                [r["timestamp"] for r in newer] == [full[-1]["timestamp"]] and skipped == 0,
                "since=2nd-newest -> only the newest record",
                describe(newer),
            )
            check(read(24, since=ts_of(full[-1])) == ([], 0), "since=newest -> no records")
            check(
                read(24, since=ts_of(full[-1]) + 3600) == ([], 0),
                "since in the future -> no records",
            )
            older, _ = read(24, initial=256, since=now - 48 * 3600)
            check(
                [r["timestamp"] for r in older] == [r["timestamp"] for r in full],
                "since older than the cutoff behaves like no since",
            )
            # The record exactly at `since` is held by the client: excluded.
            exact, _ = read(24, since=ts_of(full[-3]))
            check(len(exact) == 2, "record at exactly since is excluded", describe(exact))

            # 12. Reaching `since` covers the read: a byte cap that cannot reach
            #     the 24 h cutoff is not hit (truncated stays False) when the
            #     window already holds a record at or before `since`.
            res = server._read_history(
                24, initial_read_size=256, max_bytes=600, since=second_newest
            )
            assert not isinstance(res, str), res
            check(
                [r["timestamp"] for r in res[0]] == [full[-1]["timestamp"]] and res[2] is False,
                "since stops the tail read early (no truncation under a small byte cap)",
                f"records={len(res[0])} truncated={res[2]}",
            )
            res = server._read_history(24, initial_read_size=256, max_bytes=600)
            assert not isinstance(res, str), res
            check(res[2] is True, "the same byte cap without since does truncate")
            # Boundary: the window's first whole line is exactly the `since` record.
            lines = hist.read_bytes().splitlines(keepends=True)
            window = len(lines[-1]) + len(lines[-2]) + 10   # 10 bytes of the line before
            res = server._read_history(
                24, initial_read_size=window, max_bytes=window, since=second_newest
            )
            assert not isinstance(res, str), res
            check(
                len(res[0]) == 1 and res[2] is False,
                "a window starting exactly at the since record is covered",
                f"records={len(res[0])} truncated={res[2]}",
            )

            # 13. cutoff on a normal read is the window start.
            res = server._read_history(24)
            assert not isinstance(res, str), res
            now = datetime.now(timezone.utc).timestamp()
            check(
                abs(res[3] - (now - 24 * 3600)) < 2,
                "cutoff = now - hours on a non-empty file",
                f"cutoff={res[3]} expected~{now - 24 * 3600}",
            )
    finally:
        server.HISTORY_FILE = orig_history_file


def test_append_history_unit() -> None:
    """Appending after an interrupted write must preserve old and new records."""
    from unittest.mock import mock_open, patch
    import server

    print(f"\n{BOLD}History append recovery{RESET}\n")
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(server, "LOGS_DIR", Path(tmp)), \
            patch.object(server, "HISTORY_FILE", Path(tmp) / "history.jsonl"):
        hist = server.HISTORY_FILE
        before = _history_line(2, id="before").encode()
        complete = _history_line(1, id="complete").encode().rstrip(b"\n")
        recovered = json.loads(_history_line(0.1, id="recovered"))
        following = json.loads(_history_line(0, id="following"))

        def readable_ids() -> tuple[list[str], int]:
            result = server._read_history(24)
            assert isinstance(result, tuple), result
            return [row["id"] for row in result[0]], result[1]

        for label, original, expected, skipped in [
            ("missing", None, [], 0),
            ("empty", b"", [], 0),
            ("terminated", before, ["before"], 0),
            ("missing newline", before + complete, ["before", "complete"], 0),
            ("partial JSON", before + b'{"timestamp":"2026-', ["before"], 1),
            ("partial UTF-8", before + b'{"name":"\xe2\x82', ["before"], 1),
        ]:
            if original is None:
                hist.unlink(missing_ok=True)
            else:
                hist.write_bytes(original)
            server.append_history_record(recovered)
            server.append_history_record(following)
            prefix = original or b""
            separator = b"\n" if prefix and not prefix.endswith(b"\n") else b""
            suffix = b"".join((json.dumps(row) + "\n").encode() for row in (recovered, following))
            check(hist.read_bytes() == prefix + separator + suffix,
                  f"{label}: append preserves every existing byte")
            check(readable_ids() == (expected + ["recovered", "following"], skipped),
                  f"{label}: both acknowledged appends remain readable")

        # A real short write, isolated in a child so the test runner's files and
        # signal disposition are unaffected. Repeat it to cover failed recovery.
        hist.write_bytes(before)
        short_write = '''
import errno, resource, signal, sys
from pathlib import Path
import server
server.LOGS_DIR = Path(sys.argv[1])
server.HISTORY_FILE = server.LOGS_DIR / "history.jsonl"
limit = server.HISTORY_FILE.stat().st_size + 80
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
try:
    server.append_history_record({"timestamp": "2026-01-01T00:00:00+00:00", "pad": "x" * 8192})
except OSError as exc:
    if exc.errno != errno.EFBIG:
        raise
else:
    raise AssertionError("file-size limit did not fail the append")
'''
        for attempt in range(2):
            prior = hist.read_bytes()
            result = subprocess.run([sys.executable, "-c", short_write, tmp],
                                    cwd=Path(__file__).parent, capture_output=True, text=True, timeout=10)
            partial = hist.read_bytes()
            check(result.returncode == 0 and partial.startswith(prior)
                  and len(partial) == len(prior) + 80 and not partial.endswith(b"\n"),
                  f"short write {attempt + 1}: real failure preserves the prior bytes", result.stderr)
        server.append_history_record(recovered)
        server.append_history_record(following)
        check(hist.read_bytes().startswith(partial + b"\n")
              and readable_ids() == (["before", "recovered", "following"], 2),
              "repeated short writes: recovery keeps old and both subsequent records")

        for failure in ("seek", "read", "short-read"):
            prior = hist.read_bytes()
            opener = mock_open()
            handle = opener.return_value
            handle.seek.return_value = len(prior)
            if failure == "short-read":
                handle.read.return_value = b""
            else:
                getattr(handle, failure).side_effect = OSError("injected tail inspection failure")
            rejected = False
            with patch("server.open", opener, create=True):
                try:
                    server.append_history_record(recovered)
                except OSError:
                    rejected = True
            check(rejected and not handle.write.called and hist.read_bytes() == prior,
                  f"tail {failure} failure refuses to append or alter history")

        prior = hist.read_bytes()
        rejected = False
        try:
            server.append_history_record({"invalid": object()})
        except TypeError:
            rejected = True
        check(rejected and hist.read_bytes() == prior, "serialization failure leaves history untouched")


def test_history_export_unit() -> None:
    """A concurrent append must not change an export's bytes or HTTP framing."""
    import gzip
    import h11
    from starlette.requests import Request
    from unittest.mock import patch
    import server

    print(f"\n{BOLD}History export during recording{RESET}\n")
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(server, "LOGS_DIR", Path(tmp)), \
            patch.object(server, "HISTORY_FILE", Path(tmp) / "history.jsonl"), \
            patch.object(server, "PASSWORD_FILE", Path(tmp) / "password"):
        server.PASSWORD_FILE.write_text(server._hash_password("export-test"))
        original = _history_line(1, id="existing").encode() * 3000 + b'{"partial":"\xe2\x82'
        appended = {"timestamp": datetime.now(timezone.utc).isoformat(), "id": "new"}

        async def export(label, data, *, method="GET", headers=None, expected=None, status=200):
            server.HISTORY_FILE.write_bytes(data)
            scope = {
                "type": "http", "method": method, "path": "/api/history/raw",
                "headers": [(b"x-dashboard-password", b"export-test")],
                "query_string": b"", "client": ("127.0.0.1", 12345),
                "extensions": {},
            }
            response = await server.get_history_raw(Request(scope))
            for key, value in (headers or {}).items():
                if key == "if-range" and value == "match":
                    value = response.headers["etag"]
                scope["headers"].append((key.encode(), value.encode()))
            connection = h11.Connection(h11.SERVER)
            connection.receive_data(f"{method} /api/history/raw HTTP/1.1\r\nHost: local\r\n\r\n".encode())
            connection.next_event()
            connection.next_event()
            body, response_headers, response_status, wrote = [], {}, None, False

            async def send(message):
                nonlocal response_headers, response_status, wrote
                if message["type"] == "http.response.start":
                    response_headers = dict(message["headers"])
                    response_status = message["status"]
                    connection.send(h11.Response(status_code=response_status, headers=message["headers"]))
                else:
                    chunk = message.get("body", b"")
                    if chunk and not wrote:
                        # Append from a real worker while the download is active.
                        # This also proves the download does not hold the writer lock.
                        await asyncio.wait_for(asyncio.to_thread(server.append_history_record, appended), 2)
                        wrote = True
                    body.append(chunk)
                    connection.send(h11.Data(data=chunk))
                    if not message.get("more_body", False):
                        connection.send(h11.EndOfMessage())

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            await server._GZipUnlessRange(response, minimum_size=1000, compresslevel=1)(scope, receive, send)
            downloaded = b"".join(body)
            if response_headers.get(b"content-encoding") == b"gzip":
                downloaded = gzip.decompress(downloaded)
            wanted = b"" if method == "HEAD" else data if expected is None else expected
            separator = b"\n" if data and not data.endswith(b"\n") else b""
            disk = data + (separator + (json.dumps(appended) + "\n").encode() if wrote else b"")
            check(response_status == status and downloaded == wanted
                  and server.HISTORY_FILE.read_bytes() == disk,
                  f"{label}: exact export, valid HTTP framing, history bytes preserved")

        async def run():
            await export("full export with torn UTF-8 tail", original)
            await export("gzip export", original, headers={"accept-encoding": "gzip"})
            await export("matching If-Range", original,
                         headers={"range": "bytes=10-100000", "if-range": "match"},
                         expected=original[10:100001], status=206)
            await export("stale If-Range falls back to full export", original,
                         headers={"range": "bytes=10-100000", "if-range": '"old"'})
            await export("empty file", b"")
            await export("exact chunk boundary", b"x" * server.FileResponse.chunk_size)
            await export("small file with gzip accepted", b"{}\n", headers={"accept-encoding": "gzip"})
            # Test the response's existing HEAD behavior without changing the GET-only route.
            await export("HEAD response", original, method="HEAD")

        asyncio.run(run())


def test_rate_clamp_unit() -> None:
    """Rate caps: _cfg_num, _clamp_rate, clamp_record_rates, and the read path."""
    print(f"\n{BOLD}rate clamp unit tests{RESET}\n")
    import server

    # _cfg_num: ints and floats in range pass (as float); bool, strings, 0 and
    # non-finite fall back to the default with a warning.
    def num(v: object) -> float:
        return server._cfg_num({"k": v}, "k", 7.0, 1, 1e9, "t")

    check(num(1250) == 1250.0 and isinstance(num(1250), float), "_cfg_num int -> float")
    check(num(1250.5) == 1250.5, "_cfg_num float passes")
    check(num(True) == 7.0, "_cfg_num rejects bool")
    check(num("x") == 7.0, "_cfg_num rejects str")
    check(num(0) == 7.0, "_cfg_num rejects below lo")
    check(num(float("inf")) == 7.0 and num(float("nan")) == 7.0, "_cfg_num rejects inf/nan")
    check(server._cfg_num({}, "k", 7.0, 1, 1e9, "t") == 7.0, "_cfg_num missing -> default")

    # _read_limits: the "limits" section, not thresholds; defaults otherwise.
    check(
        server._read_limits({"limits": {"network_mbps": 100, "disk_io_mbps": 2000.5}}) == (100.0, 2000.5),
        "_read_limits reads config.json limits",
    )
    check(
        server._read_limits({"thresholds": {"network_mbps": 3, "disk_io_mbps": 50}}) == (1250.0, 15000.0),
        "_read_limits ignores thresholds (same key names)",
    )
    check(server._read_limits({}) == (1250.0, 15000.0), "_read_limits defaults without limits")
    check(
        server._read_limits({"limits": "fast"}) == (1250.0, 15000.0)
        and server._read_limits({"limits": {"network_mbps": 0}})[0] == 1250.0,
        "_read_limits: non-object section or bad value -> defaults",
    )

    check(server._clamp_rate(-3.0, 10.0) == 0.0, "_clamp_rate floors at 0")
    check(server._clamp_rate(1e9, 10.0) == 10.0, "_clamp_rate caps")
    check(server._clamp_rate(4.5, 10.0) == 4.5, "_clamp_rate passes sane values")

    rc = server._RateCaps()
    vals = [rc.sample(v * 1e6, 10.0, ("network.in_mbps", "eth0")) for v in (4.0, 50.0, -1.0, 99.0)]
    check(vals == [4.0, 10.0, 0.0, 10.0], "_RateCaps.sample: bytes/s -> MB/s, clamps without dropping", str(vals))
    odd = [rc.sample(v, 10.0, ("network.in_mbps", "eth0")) for v in (float("inf"), float("nan"), "x", None, True)]
    check(odd == [0.0] * 5, "_RateCaps.sample: missing/non-finite/non-numeric -> 0", str(odd))
    check(
        rc.seen == {("network.in_mbps", "eth0"): 4}
        and rc.hits == {("network.in_mbps", "eth0"): [50.0, 99.0]},
        "_RateCaps counts only real samples and keeps over-cap raw values",
        f"seen={rc.seen} hits={rc.hits}",
    )

    # _parse_since: encoded, unencoded '+', Z, naive (= UTC), space separator, junk.
    ref = datetime(2026, 10, 23, 12, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
    for text in ("2026-10-23T12:00:00.5+00:00", "2026-10-23T12:00:00.5 00:00",
                 "2026-10-23T12:00:00.5Z", "2026-10-23T12:00:00.5", "2026-10-23 12:00:00.5"):
        check(server._parse_since(text) == ref, f"_parse_since({text!r})")
    check(server._parse_since(None) is None and server._parse_since("") is None, "_parse_since absent -> None")
    bad = server._parse_since("garbage")
    check(getattr(bad, "status_code", None) == 400, "_parse_since junk -> 400")

    # Caps pinned here so the expectations don't depend on this box's config.json.
    orig_caps = (server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS)
    server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = 1250.0, 15000.0
    try:
        _rate_clamp_record_checks(server)
        _collect_sample_cap_checks(server)
    finally:
        server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = orig_caps


def _rate_clamp_record_checks(server: object) -> None:
    """clamp_record_rates and the read path, at the default caps."""
    net_cap, dio_cap = 1250.0, 15000.0
    spike = {"avg": 1e9, "min": -5.0, "max": 1e12}
    clamped = {"avg": net_cap, "min": 0.0, "max": net_cap}
    dclamped = {"avg": dio_cap, "min": 0.0, "max": dio_cap}

    # A record from before per-device capping (no rate_caps): only totals exist,
    # so the totals are clamped, both directions.
    legacy: dict = {
        "network": {"in_mbps": dict(spike), "out_mbps": dict(spike)},
        "disk_io": {
            "read_mbps": dict(spike),
            "write_mbps": dict(spike),
            "drives": [
                {"name": "/", "read_mbps": dict(spike), "write_mbps": dict(spike)},
                {"name": "/data", "read_mbps": {"avg": None, "min": None, "max": None}, "write_mbps": "bogus"},
                "not a dict",
            ],
        },
        "cpu": {"avg": 1e9},
    }
    server.clamp_record_rates(legacy)
    check(
        legacy["network"]["in_mbps"] == clamped and legacy["network"]["out_mbps"] == clamped,
        "legacy record: network in and out totals clamped, negatives floored",
        describe(legacy["network"]),
    )
    check(
        legacy["disk_io"]["read_mbps"] == dclamped and legacy["disk_io"]["write_mbps"] == dclamped,
        "legacy record: disk read and write totals clamped",
    )
    drives = legacy["disk_io"]["drives"]
    check(
        drives[0]["read_mbps"] == dclamped and drives[0]["write_mbps"] == dclamped
        and drives[1]["read_mbps"] == {"avg": None, "min": None, "max": None}
        and drives[1]["write_mbps"] == "bogus" and drives[2] == "not a dict",
        "per-drive read and write clamped; None stats and odd entries left alone",
    )
    check(legacy["cpu"] == {"avg": 1e9}, "other metrics untouched")

    # A per-device-capped record (rate_caps): totals are sums that may rightly
    # exceed one cap, so they are left alone; drives are still clamped.
    big = {"avg": 3000.0, "min": 2000.0, "max": 4000.0}
    new: dict = {
        "rate_caps": {"network_mbps": net_cap, "disk_io_mbps": dio_cap},
        "network": {"in_mbps": dict(big), "out_mbps": dict(big)},
        "disk_io": {
            "read_mbps": {"avg": 20000.0, "min": 0.0, "max": 25000.0},
            "write_mbps": {"avg": 1.0, "min": 1.0, "max": 1.0},
            "drives": [{"name": "/", "read_mbps": dict(spike), "write_mbps": dict(spike)}],
        },
    }
    server.clamp_record_rates(new)
    check(
        new["network"]["in_mbps"] == big and new["network"]["out_mbps"] == big
        and new["disk_io"]["read_mbps"]["max"] == 25000.0,
        "per-device-capped record: totals over one cap are left alone",
        describe(new),
    )
    check(
        new["disk_io"]["drives"][0]["read_mbps"] == spike,
        "per-device-capped record: served as recorded (drives too)",
    )
    server.clamp_record_rates({"network": None, "disk_io": 5})
    server.clamp_record_rates({})
    ok("clamp_record_rates tolerates missing/odd blocks")

    # The read path serves clamped values for legacy records and never rewrites.
    orig_history_file = server.HISTORY_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "history.jsonl"
            server.HISTORY_FILE = hist
            hist.write_text(
                _history_line(
                    1,
                    network={"in_mbps": {"avg": 1e9, "min": 1e9, "max": 1e9},
                             "out_mbps": {"avg": 0.5, "min": 0.1, "max": 0.9}},
                    disk_io={"read_mbps": {"avg": 1e9, "min": 0.0, "max": 1e9},
                             "write_mbps": {"avg": 1.0, "min": 1.0, "max": 1.0}},
                )
            )
            result = server._read_history(24)
            assert not isinstance(result, str), result
            served = result[0][0]
            check(
                served["network"]["in_mbps"]["avg"] == net_cap
                and served["network"]["out_mbps"]["max"] == 0.9
                and served["disk_io"]["read_mbps"]["max"] == dio_cap
                and served["disk_io"]["write_mbps"]["avg"] == 1.0,
                "_read_history serves clamped legacy rates, sane values untouched",
                describe(served),
            )
            raw = json.loads(hist.read_text())
            check(raw["network"]["in_mbps"]["avg"] == 1e9, "reading never rewrites the file")
    finally:
        server.HISTORY_FILE = orig_history_file


def _collect_sample_cap_checks(server: object) -> None:
    """collect_history_sample against a fake Glances: each interface and each
    drive is capped on its own (a spike counts as the cap, never dropped),
    totals are uncapped sums, bad rates are ignored without losing the record,
    and the record lists what hit a cap; the log line is for recorded samples
    only. Caps are lowered so the numbers are easy to read."""
    server.MAX_NETWORK_MBPS, server.MAX_DISK_IO_MBPS = 100.0, 200.0
    M = 1e6  # MB/s -> bytes/s
    calls = {"network": 0, "diskio": 0}
    # Per sample (3 samples), MB/s. eth0 spikes in (sample 2) and out (sample 3);
    # docker0 carries steady traffic that also crossed eth0, so the in total of
    # sample 2 (100 + 80) rightly exceeds the 100 cap and is not capped; wlan0
    # reports Infinity and a string, which are ignored.
    eth0_in, eth0_out = [1, 5e6, 2], [1, 1, 3e6]
    docker_in = [80, 80, 80]
    wlan0_in = [float("inf"), "x", 1]
    sda1_read, sda1_write = [1, 9e8, 1], [0, 0, 7e8]
    sdb_read = [150, 150, 150]   # mounted: total read in sample 2 = 200 + 150

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/4/network":
            i = calls["network"] % 3
            calls["network"] += 1
            body = json.dumps([
                {"interface_name": "eth0", "bytes_recv_rate_per_sec": eth0_in[i] * M,
                 "bytes_sent_rate_per_sec": eth0_out[i] * M},
                {"interface_name": "docker0", "bytes_recv_rate_per_sec": docker_in[i] * M,
                 "bytes_sent_rate_per_sec": 0},
                {"interface_name": "wlan0",
                 "bytes_recv_rate_per_sec": wlan0_in[i] * M if isinstance(wlan0_in[i], (int, float)) else wlan0_in[i],
                 "bytes_sent_rate_per_sec": None},
                {"interface_name": "lo", "bytes_recv_rate_per_sec": 9e15, "bytes_sent_rate_per_sec": 9e15},
            ])  # json.dumps writes Infinity, as Glances' stdlib json would
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})
        if path == "/api/4/diskio":
            i = calls["diskio"] % 3
            calls["diskio"] += 1
            rows = [
                {"disk_name": "sda1", "read_bytes_rate_per_sec": sda1_read[i] * M,
                 "write_bytes_rate_per_sec": sda1_write[i] * M},
                {"disk_name": "sdb", "read_bytes_rate_per_sec": sdb_read[i] * M, "write_bytes_rate_per_sec": 0},
                {"disk_name": "md0", "read_bytes_rate_per_sec": 9e15, "write_bytes_rate_per_sec": 0},
            ]
            if i == 1:  # appears for one sample only
                rows.append({"disk_name": "sdc", "read_bytes_rate_per_sec": 5e14, "write_bytes_rate_per_sec": 0})
            return httpx.Response(200, json=rows)
        if path == "/api/4/fs":
            return httpx.Response(200, json=[
                {"device_name": "/dev/sda1", "mnt_point": "/", "size": 100, "used": 50, "percent": 50},
                {"device_name": "/dev/sdb", "mnt_point": "/data", "size": 100, "used": 10, "percent": 10},
            ])
        if path == "/api/4/cpu":
            return httpx.Response(200, json={"total": 5.0})
        return httpx.Response(404, json={})

    orig_client = server.httpx.AsyncClient
    orig = {n: getattr(server, n) for n in ("get_gpu_stats", "_get_ip_info")}
    state_keys = ("history_sample_duration", "history_sample_count", "history_processes_enabled",
                  "history_process_count", "ping_enabled")
    orig_state = {k: getattr(server.APP.state, k, None) for k in state_keys}

    class FakeClient(orig_client):  # type: ignore[misc, valid-type]
        def __init__(self, *a: object, **k: object) -> None:
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    cap_log = Capture()
    server.logger.addHandler(cap_log)
    try:
        server.httpx.AsyncClient = FakeClient
        server.get_gpu_stats = lambda: []
        server._get_ip_info = lambda: {}
        for k, v in zip(state_keys, (0, 3, False, 10, False)):
            setattr(server.APP.state, k, v)
        rec = asyncio.run(server.collect_history_sample("http://glances.invalid"))
        recorded_log = [m for m in cap_log.messages if m.startswith("Rate cap hit")]
        cap_log.messages.clear()
        calls.update(network=1, diskio=1)   # the live sample is the spiky one
        live = asyncio.run(server.collect_history_sample("http://glances.invalid", live=True))
        live_log = [m for m in cap_log.messages if m.startswith("Rate cap hit")]
    finally:
        server.logger.removeHandler(cap_log)
        server.httpx.AsyncClient = orig_client
        for n, f in orig.items():
            setattr(server, n, f)
        for k, v in orig_state.items():
            setattr(server.APP.state, k, v)

    # in totals: 1+80+0, 100+80+0, 2+80+1  (eth0 capped; wlan0 bad values -> 0)
    check(
        rec.get("network", {}).get("in_mbps") == {"avg": round(344 / 3, 2), "min": 81.0, "max": 180.0},
        "network: each interface capped, total is their uncapped sum, bad rates ignored",
        describe(rec.get("network")),
    )
    check(
        rec.get("network", {}).get("out_mbps", {}).get("max") == 100.0,
        "network out capped per interface too",
        describe(rec.get("network", {}).get("out_mbps")),
    )
    drives = {d["name"]: d for d in rec.get("disk_io", {}).get("drives", [])}
    check(
        drives.get("/", {}).get("read_mbps", {}).get("max") == 200.0
        and drives.get("/", {}).get("write_mbps", {}).get("max") == 200.0
        and drives.get("/data", {}).get("read_mbps", {}).get("max") == 150.0,
        "per-drive read and write capped",
        describe(drives),
    )
    check(
        rec.get("disk_io", {}).get("read_mbps", {}).get("max") == 350.0,
        "disk total is the uncapped sum of capped drives (200 + 150)",
        describe(rec.get("disk_io", {}).get("read_mbps")),
    )
    check(
        rec.get("rate_caps") == {"network_mbps": 100.0, "disk_io_mbps": 200.0},
        "record notes the caps it was taken under (rate_caps)",
        describe(rec.get("rate_caps")),
    )
    expected = [
        {"metric": "disk_io.read_mbps", "device": "md0", "samples": 3, "of": 3, "raw_max": 9e9, "cap": 200.0},
        {"metric": "disk_io.read_mbps", "device": "sda1", "drive": "/", "samples": 1, "of": 3, "raw_max": 9e8,
         "cap": 200.0},
        {"metric": "disk_io.read_mbps", "device": "sdc", "samples": 1, "of": 1, "raw_max": 5e8, "cap": 200.0},
        {"metric": "disk_io.write_mbps", "device": "sda1", "drive": "/", "samples": 1, "of": 3, "raw_max": 7e8,
         "cap": 200.0},
        {"metric": "network.in_mbps", "interface": "eth0", "samples": 1, "of": 3, "raw_max": 5e6, "cap": 100.0},
        {"metric": "network.out_mbps", "interface": "eth0", "samples": 1, "of": 3, "raw_max": 3e6, "cap": 100.0},
    ]
    check(
        rec.get("rate_capped") == expected,
        "rate_capped: one entry per interface/device, its own sample count, raw max",
        json.dumps(rec.get("rate_capped")),
    )
    check(len(recorded_log) == 1, "one 'Rate cap hit' log line per recorded sample", str(recorded_log))
    check(
        live.get("rate_capped") and not live_log,
        "live (/text) samples note caps but never log",
        f"capped={live.get('rate_capped')} log={live_log}",
    )


def test_history_fetch_js() -> None:
    """The browser's history cache (static/app.js fetchHistoryData and helpers),
    run in node against a scripted safeFetch."""
    print(f"\n{BOLD}history fetch (app.js, node) unit tests{RESET}\n")
    node = shutil.which("node")
    if not node:
        # A silent skip would read as a pass; opt out explicitly instead.
        if os.environ.get("HORATIO_SKIP_NODE") == "1":
            print("  SKIP  history fetch JS tests (HORATIO_SKIP_NODE=1, node not installed)")
        else:
            fail("history fetch JS tests", "node not installed (set HORATIO_SKIP_NODE=1 to skip)")
        return
    src = (Path(__file__).parent / "static" / "app.js").read_text()
    cfg_src = (Path(__file__).parent / "static" / "config.js").read_text()

    def between(text: str, start: str, end: str) -> str:
        # The test slices app.js / config.js by these markers; a renamed one is
        # a test failure that names it, not a crash of the whole suite.
        a = text.find(start)
        b = text.find(end, a + 1)
        if a < 0 or b < 0:
            raise LookupError(f"marker not found: {start if a < 0 else end!r}")
        return text[a:b]


    try:
        harness = r"""
const assert = require('assert');
let defaultHistoryHours = 24;
const toasts = [];
function showError(m) { toasts.push(m); }
console.warn = () => {};
const calls = [];
let responder = null;
async function safeFetch(url) { calls.push(url); return responder(url); }
const historyCards = new Map();
const document = { querySelector: () => null, getElementById: () => null, querySelectorAll: () => [] };
const HISTORY_METRICS = { cpu: {}, memory: {}, temps: { sideField: 'temps' } };
const draws = [];
function createHistoryChart(canvasId, metric, records, unitType, hours) {
  draws.push([metric, hours]);
  return { records, unitType, data: { datasets: [] }, destroy() {}, update() {} };
}
let thresholds = { network_mbps: 3 }, dashboardConfig = {};
let configBody = null;
""" + between(src, "const historyFetchInflight", "// Markup for a failed history fetch") \
        + between(src, "function historyErrorHTML", "// Extract metric value from a history record") \
        + between(src, "async function loadHistoryData", "// Show history for all metrics") \
        + between(src, "async function refreshAllHistory", "document.getElementById('refresh-history-btn')?.addEventListener") \
        + between(cfg_src, "// Ceilings (MB/s)", "// Read a CSS custom property") \
        + between(cfg_src, "async function loadConfig", "// Update card warning state") + r"""
const MS = (min) => Date.UTC(2026, 9, 23, 12, min);
const T = (min) => new Date(MS(min)).toISOString().replace('Z', '+00:00');
const recs = (...mins) => mins.map(m => ({ timestamp: T(m) }));
// A server response for a window of `hours` whose clock reads T(nowMin).
const resp = (nowMin, records, extra = {}, hours = 24) => ({ ok: true, data: {
  records, count: records.length, skipped_lines: 0, truncated: false,
  cutoff: MS(nowMin) / 1000 - hours * 3600, now: MS(nowMin) / 1000, ...extra } });
const since = (url) => { const m = url.match(/since=([^&]+)/); return m ? decodeURIComponent(m[1]) : null; };
const ok = (r) => Promise.resolve(r);
const deferred = () => { let r; const p = new Promise(res => { r = res; }); return [p, r]; };
const card = (hours) => {
  const range = { value: String(hours) }, container = { innerHTML: '' };
  return { card: { dataset: { historyId: 'test' }, remove() {},
    querySelector: selector => selector === '.history-range-input' ? range : container },
    chart: null, loadSeq: 0 };
};
(async () => {
  // 1. First load: a full fetch.
  responder = () => ok(resp(25, recs(0, 20, 10)));
  let d = await fetchHistoryData(24);
  assert.strictEqual(since(calls.at(-1)), null, 'first fetch has no since');
  const firstObj = d;
  // 2. Any later fetch of a cached window (a reopened card, a range changed
  //    back, Refresh) tops it up: since = the server's last now - 5 min. The
  //    late record (15) merges in time order, held ones dedupe, records before
  //    the server's cutoff drop, and truncated keeps its window meaning.
  historyCache.get(24).truncated = true;
  responder = () => ok(resp(30, recs(10, 15, 20, 30), { cutoff: MS(5) / 1000 }));
  d = await fetchHistoryData(24);
  assert.strictEqual(Date.parse(since(calls.at(-1))), MS(25) - 300 * 1000, 'reopen tops up from last now - 5 min');
  assert.deepStrictEqual(d.records.map(r => r.timestamp), [T(10), T(15), T(20), T(30)], 'merged, deduped, sorted, trimmed');
  assert.strictEqual(d.count, 4);
  assert.strictEqual(d.truncated, false, 'truncation clears after its missing prefix ages out');
  assert.strictEqual(firstObj.records.length, 3, 'the cached object a chart holds is not mutated');
  const truncatedBase = { records: recs(0, 10), truncated: true };
  for (const [cutoffMin, expected] of [[-5, true], [0, false], [5, false]]) {
    const merged = mergeHistoryDelta(truncatedBase, resp(20, recs(20), { cutoff: MS(cutoffMin) / 1000 }).data);
    assert.strictEqual(merged.truncated, expected, `truncation boundary at ${cutoffMin}`);
  }
  assert.strictEqual(mergeHistoryDelta({ records: [], truncated: true }, resp(20, recs(20)).data).truncated,
                     true, 'an empty cache cannot establish that the missing prefix aged out');
  // 3. Record timestamps never set since: a future-stamped record changes nothing.
  responder = () => ok(resp(40, recs(35, 400)));
  await fetchHistoryData(24, { force: true });
  responder = () => ok(resp(45, recs(42)));
  await fetchHistoryData(24, { force: true });
  assert.strictEqual(Date.parse(since(calls.at(-1))), MS(40) - 300 * 1000, 'since from the server clock, not records');
  // 4. A window with corrupt lines refetches in full; a clean one tops up again.
  responder = () => ok(resp(5, recs(0), { skipped_lines: 3 }, 12));
  await fetchHistoryData(12);
  responder = () => ok(resp(6, recs(0, 5), {}, 12));
  d = await fetchHistoryData(12, { force: true });
  assert.strictEqual(since(calls.at(-1)), null, 'window with skipped lines -> full refetch');
  assert.strictEqual(d.skipped_lines, 0);
  responder = () => ok(resp(8, recs(7), {}, 12));
  await fetchHistoryData(12, { force: true });
  assert.notStrictEqual(since(calls.at(-1)), null, 'clean window -> top-up again');
  // 5. A backend that reports no clock (OpenWrt): always a full fetch, which replaces.
  responder = () => ok({ ok: true, data: { records: recs(1, 2), count: 2, skipped_lines: 0 } });
  await fetchHistoryData(6);
  responder = () => ok({ ok: true, data: { records: recs(2, 3), count: 2, skipped_lines: 0 } });
  d = await fetchHistoryData(6, { force: true });
  assert.strictEqual(since(calls.at(-1)), null, 'no now -> no since');
  assert.deepStrictEqual(d.records.map(r => r.timestamp), [T(2), T(3)], 'full window replaces');
  let before = calls.length;
  d = await fetchHistoryData(6);
  assert.strictEqual(calls.length, before, 'no clock: a toggle / reopen is served from the cache');
  assert.strictEqual(d.count, 2);
  // ...and a since= response without cutoff (an older server) is a full window too.
  assert.deepStrictEqual(mergeHistoryDelta({ records: recs(0) }, { records: recs(5), count: 1 }).records,
                         recs(5), 'no cutoff -> replace');
  // 6. A truncated top-up (gap) refetches in full, without since.
  let n = 0;
  responder = (url) => { n++; return since(url)
    ? ok(resp(50, recs(50), { truncated: true })) : ok(resp(50, recs(30, 40, 50))); };
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(n, 2, 'truncated delta -> second, full request');
  assert.strictEqual(since(calls.at(-1)), null);
  assert.strictEqual(d.count, 3);
  // 7. A card opened during a Refresh joins it.
  const [p, resolve] = deferred();
  responder = () => p;
  before = calls.length;
  const refreshing = fetchHistoryData(24, { force: true });
  const opened = fetchHistoryData(24);
  assert.strictEqual(calls.length - before, 1, 'non-force caller shares the in-flight request');
  resolve(resp(60, recs(40, 50, 60)));
  assert.strictEqual((await opened).count, 4);
  assert.strictEqual((await refreshing).count, 4);
  // 8. A fetch overtaken by a Refresh does not overwrite the Refresh's result.
  const [p3, resolve3] = deferred();
  responder = () => p3;
  const slow = fetchHistoryData(24);
  responder = () => ok(resp(70, recs(70)));
  await fetchHistoryData(24, { force: true });
  resolve3(resp(65, recs(65)));
  await slow;
  assert.strictEqual(historyCache.get(24).now, MS(70) / 1000, 'superseded fetch is not cached');
  // 9. The last card closes during a Refresh (the real removeHistoryCard), then a
  //    card reopens: it starts its own fetch, which is cached; the old one isn't.
  historyCards.set('cpu', card(24));
  const [p2, resolve2] = deferred();
  responder = () => p2;
  const late = fetchHistoryData(24, { force: true });
  removeHistoryCard('cpu');
  assert.strictEqual(historyCache.size, 0, 'closing the last card clears the cache');
  responder = () => ok(resp(90, recs(80, 90)));
  const reopened = fetchHistoryData(24);
  assert.notStrictEqual(reopened, late, 'a reopened card does not join the discarded fetch');
  await reopened;
  resolve2(resp(75, recs(75)));
  await late;
  assert.strictEqual(historyCache.get(24)?.count, 2, 'the reopened fetch is cached, the late one is not');
  // 10. A failed fetch keeps the cached window.
  responder = () => ok({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(d.ok, false);
  assert.strictEqual(historyCache.get(24).count, 2, 'failure is not cached; window kept');
  // ...and a non-Refresh fetch that fails (a toggle or a card opened while the
  // server restarts) keeps drawing the cached window instead of an error box.
  d = await fetchHistoryData(24);
  assert.strictEqual(d.ok, true, 'failed top-up falls back to the cached window');
  assert.strictEqual(d.count, 2);
  // The same fallback applies when the delta succeeded but was truncated,
  // requiring a full read which then failed. Explicit Refresh still reports it.
  const cached = historyCache.get(24);
  responder = url => since(url) ? ok(resp(95, recs(95), { truncated: true }))
    : ok({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  before = calls.length;
  d = await fetchHistoryData(24);
  assert.strictEqual(calls.length - before, 2, 'truncated top-up attempted the full fallback');
  assert.strictEqual(d, cached, 'failed full fallback returns the cached window');
  d = await fetchHistoryData(24, { force: true });
  assert.strictEqual(d.ok, false, 'explicit Refresh reports failed full fallback');
  assert.strictEqual(d.errorType, 'timeout');
  assert.strictEqual(historyCache.get(24), cached, 'failed full fallback does not replace the cache');
  const fallbackCard = card(24);
  fallbackCard.unitType = 'gb';
  historyCards.set('memory', fallbackCard);
  await loadHistoryData('memory', 24);
  assert.strictEqual(fallbackCard.chart?.unitType, 'gb', 'unit toggle redraws cached data after full fallback failure');
  assert.strictEqual(fallbackCard.chart.records, cached.records);
  // A toggle and a newly opened card joining a forced fetch have their own
  // failure policy, including when the forced request retries a truncated delta.
  for (const fullRetry of [false, true]) {
    historyCards.clear(); historyCards.set('memory', fallbackCard);
    fallbackCard.unitType = 'percent';
    responder = () => ok(resp(90, recs(80, 90)));
    await loadHistoryData('memory', 24);
    const [failing, finishFailure] = deferred();
    responder = url => fullRetry && since(url) ? ok(resp(95, recs(95), { truncated: true })) : failing;
    before = calls.length;
    const forceCaller = fetchHistoryData(24, { force: true });
    fallbackCard.unitType = 'gb';
    const toggleCaller = loadHistoryData('memory', 24);
    const joiningCard = card(24);
    historyCards.set('cpu', joiningCard);
    const openCaller = loadHistoryData('cpu', 24);
    finishFailure({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
    assert.strictEqual((await forceCaller).ok, false, 'forced caller still receives the failure');
    await Promise.all([toggleCaller, openCaller]);
    assert.strictEqual(calls.length - before, fullRetry ? 2 : 1, 'joining cards share network work');
    assert.strictEqual(fallbackCard.chart.unitType, 'gb', 'joining toggle redraws cached data in the selected unit');
    assert.deepStrictEqual(joiningCard.chart?.records, recs(80, 90), 'joining card draws the cached window');
  }
  // Side fields have independent caches; a joined failure must use the same key.
  responder = () => ok(resp(90, recs(85), { field: 'temps' }));
  const cachedTemps = await fetchHistoryData(24, { field: 'temps' });
  const [tempsFailure, finishTemps] = deferred();
  responder = () => tempsFailure;
  const forceTemps = fetchHistoryData(24, { force: true, field: 'temps' });
  const joiningTemps = fetchHistoryData(24, { field: 'temps' });
  finishTemps({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  assert.strictEqual((await forceTemps).ok, false);
  assert.strictEqual(await joiningTemps, cachedTemps, 'joined side-field failure uses its own cached data');
  // With no cache, joining a failed forced fetch still reports an error.
  const [uncachedFailure, finishUncached] = deferred();
  responder = () => uncachedFailure;
  const forceUncached = fetchHistoryData(48, { force: true });
  const joiningUncached = fetchHistoryData(48);
  finishUncached({ ok: false, errorType: 'timeout', data: { records: [], count: 0 } });
  assert.strictEqual((await forceUncached).ok, false);
  assert.strictEqual((await joiningUncached).ok, false, 'no cached data means no fallback');
  // 11. Refresh skips a card whose range changed while its fetch was in flight
  //     (the real refreshAllHistory); the others redraw.
  const cpu = card(24), mem = card(24);
  historyCards.clear(); historyCards.set('cpu', cpu); historyCards.set('memory', mem);
  draws.length = 0;
  const [p4, resolve4] = deferred();
  responder = () => p4;
  const refresh = refreshAllHistory();
  cpu.card.querySelector('.history-range-input').value = '1';   // the user types 1 h meanwhile
  resolve4(resp(95, recs(95)));
  await refresh;
  assert.deepStrictEqual(draws, [['memory', 24]], 'only the unchanged card redraws at the old range');
  // 12. Exercise the complete Refresh -> close -> reopen -> late response path,
  //     including the actual loadHistoryData drawing gate, not just the cache.
  historyCards.clear(); historyCache.clear(); draws.length = 0;
  historyCards.set('cpu', card(24));
  responder = () => ok(resp(100, recs(90, 100)));
  await loadHistoryData('cpu', 24);
  const [p5, resolve5] = deferred();
  responder = () => p5;
  const oldRefresh = refreshAllHistory();
  removeHistoryCard('cpu');
  const newCard = card(24);
  historyCards.set('cpu', newCard);
  responder = () => ok(resp(110, recs(100, 110)));
  await loadHistoryData('cpu', 24);
  const newChart = newCard.chart;
  resolve5(resp(105, recs(100, 105)));
  await oldRefresh;
  assert.strictEqual(newCard.chart, newChart, 'old Refresh cannot redraw the reopened card');
  assert.deepStrictEqual(newCard.chart.records, recs(100, 110), 'reopened chart keeps the newer data');
  assert.deepStrictEqual(historyCache.get(24).records, recs(100, 110), 'reopened cache keeps the newer data');
  assert.strictEqual(draws.length, 2, 'only the two card opens draw');
  // 13. A newer Refresh supersedes the old one at start, regardless of which
  //     response finishes first. An old result must not suppress the new one.
  for (const oldFirst of [true, false]) {
    const [older, finishOlder] = deferred(), [newer, finishNewer] = deferred();
    responder = () => older;
    const olderRefresh = refreshAllHistory();
    responder = () => newer;
    const newerRefresh = refreshAllHistory();
    const previousChart = newCard.chart;
    const minute = oldFirst ? 120 : 130;
    if (oldFirst) {
      finishOlder(resp(minute - 5, recs(minute - 5)));
      await olderRefresh;
      assert.strictEqual(newCard.chart, previousChart, 'superseded Refresh does not draw even when it finishes first');
    }
    finishNewer(resp(minute, recs(minute)));
    await newerRefresh;
    const freshChart = newCard.chart;
    assert.strictEqual(freshChart.records.at(-1).timestamp, T(minute), 'newest Refresh draws its result');
    if (!oldFirst) {
      finishOlder(resp(minute - 5, recs(minute - 5)));
      await olderRefresh;
      assert.strictEqual(newCard.chart, freshChart, 'late superseded Refresh cannot overwrite the fresh chart');
    }
  }
  // 14. A clock rewind expands the window's older edge and can stamp new
  // records before the previous watermark. Refetch in full instead of losing
  // those records; either response clock field is enough to detect the rewind.
  for (const metadata of ['both', 'now', 'cutoff']) {
    historyCache.delete(1);
    let clockMin = 100, timeline = recs(20, 50, 100), rewound = false;
    responder = url => {
      const watermark = since(url);
      const records = timeline.filter(r => Date.parse(r.timestamp) >= MS(clockMin) - 3600000 &&
        (!watermark || Date.parse(r.timestamp) > Date.parse(watermark)));
      const response = resp(clockMin, records, {}, 1);
      if (rewound && metadata === 'now') delete response.data.cutoff;
      if (rewound && metadata === 'cutoff') delete response.data.now;
      return ok(response);
    };
    d = await fetchHistoryData(1);
    assert.deepStrictEqual(d.records, recs(50, 100));
    clockMin = 70; timeline = recs(20, 50, 60, 65, 100); rewound = true;
    before = calls.length;
    d = await fetchHistoryData(1, { force: true });
    assert.strictEqual(calls.length - before, 2, `${metadata} rewind triggers a full retry`);
    assert.strictEqual(since(calls.at(-1)), null, 'rewind retry has no watermark');
    assert.deepStrictEqual(d.records, timeline, 'full retry restores the older edge and newly backdated samples');
    if (metadata === 'both') {
      clockMin = 75; timeline = recs(20, 50, 60, 65, 73, 100);
      before = calls.length;
      d = await fetchHistoryData(1);
      assert.strictEqual(calls.length - before, 1, 'incremental reads resume after the rewind');
      assert.strictEqual(Date.parse(since(calls.at(-1))), MS(65), 'next watermark uses the corrected server clock');
      assert.deepStrictEqual(d.records, timeline);
    }
  }
  // 15. config.js: limits from /config.json, validated like the server's _cfg_num.
  responder = () => ({ ok: true, data: configBody });
  configBody = { thresholds: { network_mbps: 9 }, limits: { network_mbps: 5, disk_io_mbps: 'x', bogus: 7 } };
  await loadConfig();
  assert.deepStrictEqual(limits, { network_mbps: 5, disk_io_mbps: 15000 }, 'valid limit taken, bad one ignored');
  for (const bad of [0, -1, true, 1e10, null, Infinity]) {
    configBody = { limits: { network_mbps: bad } };
    await loadConfig();
    assert.strictEqual(limits.network_mbps, 5, `limit ${bad} rejected`);
  }
  assert.deepStrictEqual([clampRate(-3, 10), clampRate(50, 10), clampRate(4, 10), clampRate(NaN, 10), clampRate(undefined, 10)],
                         [0, 10, 4, 0, 0], 'clampRate');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    except LookupError as e:
        fail("history fetch JS tests", str(e))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        script = f.name
    try:
        r = subprocess.run([node, script], capture_output=True, text=True, timeout=30)
        check(
            r.returncode == 0 and r.stdout.strip() == "OK",
            "history cache (top-up on reopen, order, server clock, skipped, no clock, gap, "
            "in-flight, superseded, close/reopen, per-caller failure + full-read cached fallback, "
            "clock rewind, aged-out truncation), "
            "refresh vs range change/reopen/newer refresh, "
            "config.js limits",
            (r.stderr or r.stdout).strip()[-600:],
        )
    finally:
        os.unlink(script)


def test_auth_throttle_unit() -> None:
    """_verify_password_throttled under a concurrent burst: the lockout counts
    attempts before the KDF, so only AUTH_MAX_FAILS guesses reach it, one at a
    time, and the rest get 429 at once."""
    print(f"\n{BOLD}Unit: password lockout under a burst{RESET}\n")
    import types

    import server

    running = {"now": 0, "max": 0, "calls": 0}

    def slow_wrong(password: str) -> bool:
        running["now"] += 1
        running["calls"] += 1
        running["max"] = max(running["max"], running["now"])
        time.sleep(0.05)
        running["now"] -= 1
        return password == "right"

    orig = server._verify_password
    server._verify_password = slow_wrong
    server._auth_failures.clear()
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="198.51.100.7"))

    async def burst() -> list:
        return await asyncio.gather(*(server._verify_password_throttled(req, "wrong") for _ in range(20)))

    try:
        codes = sorted(r.status_code for r in asyncio.run(burst()))
        n = server.AUTH_MAX_FAILS
        check(
            codes == [403] * n + [429] * (20 - n) and running["calls"] == n and running["max"] == 1,
            f"20 concurrent wrong guesses: {n} checked (one at a time), the rest locked out",
            f"codes={codes} kdf_calls={running['calls']} max_concurrent={running['max']}",
        )
        server._auth_failures.clear()
        ok_resp = asyncio.run(server._verify_password_throttled(req, "right"))
        check(ok_resp is None and req.client.host not in server._auth_failures,
              "a correct password passes and clears the streak")
    finally:
        server._verify_password = orig
        server._auth_failures.clear()


def test_subprocess_decoding_unit() -> None:
    """pidstat output with a process name cut mid-character must not raise
    (the Disk I/O Processes card errored on every capture)."""
    print(f"\n{BOLD}Unit: non-UTF-8 subprocess output{RESET}\n")
    import server

    orig_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "pidstat"
        fake.write_text("#!/bin/sh\nprintf 'Average:  1000  4242  12.00  3.00  0.00  caf\\303\\n'\n")
        fake.chmod(0o755)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{orig_path}"
        try:
            out = server._run_pidstat(1, 5).stdout
            check("caf�" in out, "_run_pidstat: invalid UTF-8 replaced, not raised", repr(out))
        except Exception as e:
            fail("_run_pidstat with non-UTF-8 output", repr(e))
        finally:
            os.environ["PATH"] = orig_path


def test_gpu_info_partial_unit() -> None:
    """Unsupported nvidia-smi fields must not hide usable GPU metadata."""
    from unittest.mock import patch
    import server

    print(f"\n{BOLD}partial GPU information unit tests{RESET}\n")
    output = (
        "0, NVIDIA H100 80GB HBM3, [N/A], 87, 81559, 30, 141, 700, 580.65.06\n"
        "1, NVIDIA Virtual GPU, 0, N/A, 8192, [Not Supported], nan, inf, 580.65.06\n"
        "bad, invalid index, 0, 0, 8192, 30, 0, 100, 580.65.06\n"
    )
    with patch.object(server.subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 0, output, ""),
        subprocess.CompletedProcess([], 0, "CUDA Version: 13.0\n", ""),
    ]):
        gpus = server._get_gpu_info()
    check([g["index"] for g in gpus] == [0, 1],
          "GPU info: unavailable metrics preserve rows; invalid index is skipped")
    if len(gpus) != 2:
        return
    check(gpus[0]["utilization"] is None and gpus[0]["memory_used"] == 87
          and gpus[0]["memory_total"] == 81559 and gpus[0]["temperature"] == 30,
          "GPU info: MIG utilization N/A preserves memory and temperature")
    check(gpus[0]["name"] == "NVIDIA H100 80GB HBM3"
          and gpus[0]["driver_version"] == "580.65.06" and gpus[0]["cuda_version"] == "13.0",
          "GPU info: partial readings preserve device identity and versions")
    check(gpus[1]["utilization"] == 0 and gpus[1]["memory_total"] == 8192
          and gpus[1]["memory_used"] is None and gpus[1]["temperature"] is None,
          "GPU info: genuine zero and known capacity survive other unavailable fields")
    check(gpus[1]["power_draw"] is None and gpus[1]["power_limit"] is None,
          "GPU info: non-finite metrics stay unavailable")
    check(json.loads(json.dumps(gpus, allow_nan=False)) == gpus,
          "GPU info: partial readings serialize as valid JSON")


def test_gpu_stats_compatibility_unit() -> None:
    """An unsupported optional counter must not discard basic GPU readings."""
    from unittest.mock import patch
    import server

    print(f"\n{BOLD}GPU query compatibility unit tests{RESET}\n")
    basic = "0, 0, 4096, 24576, 53, 123, 86"
    modern = subprocess.CompletedProcess([], 0, basic + ", 2500000, 1000000\n", "")
    with patch.object(server.subprocess, "run", return_value=modern) as run:
        gpus = server.get_gpu_stats()
    check(run.call_count == 1 and len(gpus) == 1
          and gpus[0]["throttle_sw_thermal_us"] == 2500000
          and gpus[0]["throttle_hw_thermal_us"] == 1000000,
          "GPU stats: modern drivers retain counters with one query")

    # Different CLI versions can put the invalid-field diagnostic on either stream.
    for counter, stream in (("sw", "stderr"), ("hw", "stdout")):
        error = f'Field "clocks_event_reasons_counters.{counter}_thermal_slowdown" is not a valid field to query.\n'
        rejected = subprocess.CompletedProcess([], 2, error if stream == "stdout" else "",
                                              error if stream == "stderr" else "")
        fallback = subprocess.CompletedProcess([], 0, basic + "\n", "")
        with patch.object(server.subprocess, "run", side_effect=[rejected, fallback]) as run:
            gpus = server.get_gpu_stats()
        check(run.call_count == 2 and len(gpus) == 1
              and gpus[0]["util_percent"] == 0 and gpus[0]["memory_used_mb"] == 4096
              and gpus[0]["memory_total_mb"] == 24576 and gpus[0]["temperature"] == 53
              and gpus[0]["temperature_memory"] == 86 and gpus[0]["power_draw"] == 123
              and gpus[0]["throttle_sw_thermal_us"] is None
              and gpus[0]["throttle_hw_thermal_us"] is None
              and "clocks_event_reasons_counters" not in run.call_args.args[0][1]
              and "temperature.memory" in run.call_args.args[0][1],
              f"GPU stats: unsupported {counter} counter on {stream} preserves basic readings")

    for label, failure in (
        ("driver error", subprocess.CompletedProcess([], 9, "", "driver not loaded")),
        ("unrelated invalid field", subprocess.CompletedProcess([], 2, "", 'Field "other" is not a valid field to query.')),
    ):
        with patch.object(server.subprocess, "run", return_value=failure) as run:
            gpus = server.get_gpu_stats()
        check(gpus == [] and run.call_count == 1, f"GPU stats: {label} does not retry")
    with patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)) as run:
        gpus = server.get_gpu_stats()
    check(gpus == [] and run.call_count == 1, "GPU stats: timeout does not retry")

    failure = subprocess.CompletedProcess([], 9, "", "fallback driver failure")
    with patch.object(server.subprocess, "run", side_effect=[rejected, failure]) as run, \
            patch.object(server.logger, "warning") as warning:
        gpus = server.get_gpu_stats()
    check(gpus == [] and run.call_count == 2 and warning.call_args.args[-1] == failure.stderr,
          "GPU stats: failed fallback is reported without further retries")


def test_system_info_unit() -> None:
    """CPU topology and address-family fallbacks, without hardware or network."""
    from unittest.mock import patch
    import server

    print(f"\n{BOLD}System information unit tests{RESET}\n")
    # Core IDs repeat across sockets and SMT siblings; field order is arbitrary.
    multi = "\n\n".join(
        f"processor : {i}\ncore id : {(i % 4) // 2}\nphysical id : {i // 4}\n"
        "model name : Fixture CPU\ncache size : 32768 KB"
        for i in range(8)
    )
    no_socket = "\n\n".join(f"processor : {i}\ncore id : {i // 2}" for i in range(4))
    no_topology = "processor : 0\n\nprocessor : 1\n"
    for label, cpuinfo, physical, logical in (
        ("multiple sockets with SMT", multi, 4, 8),
        ("missing socket IDs", no_socket, 2, 4),
        ("missing core IDs", no_topology, 2, 2),
    ):
        with patch.object(server.Path, "exists", return_value=True), \
                patch.object(server.Path, "read_text", side_effect=[cpuinfo, "32M\n"]):
            info = server._get_cpu_info()
        check(info["physical_cores"] == physical and info["logical_cores"] == logical
              and info["l3_cache"] == "32M"
              and (label != "multiple sockets with SMT"
                   or (info["model"], info["cache_size"]) == ("Fixture CPU", "32768 KB")),
              f"CPU info: {label}", describe(info))

    for label, first, second, expected in (
        ("IPv4 fallback after first provider fails", httpx.ConnectTimeout("offline"),
         "198.51.100.9", {"ipv4": "198.51.100.9"}),
        ("both address families", "198.51.100.9", "2001:db8::9",
         {"ipv4": "198.51.100.9", "ipv6": "2001:db8::9"}),
        ("IPv4-only providers keep the first address", "198.51.100.9", "198.51.100.10",
         {"ipv4": "198.51.100.9"}),
        ("both providers fail", httpx.ConnectTimeout("offline"), httpx.ConnectTimeout("offline"),
         {"error": "lookup failed"}),
    ):
        with patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")), \
                patch.object(server.httpx, "Client") as client:
            client.return_value.__enter__.return_value.get.side_effect = [
                httpx.Response(200, text=value) if isinstance(value, str) else value
                for value in (first, second)
            ]
            info = server._get_ip_info()
        check(info["public"] == expected, f"IP info: {label}", describe(info["public"]))


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


def test_gpu_processes_unit() -> None:
    """_get_gpu_processes against a fake nvidia-smi on PATH (real subprocess, so
    the decoding is exercised): the (gpu index, pid) join of compute-apps with
    pmon, a pmon command name that ends mid-character, and a stalled pmon that
    must cost only the utilization columns, quickly."""
    print(f"\n{BOLD}Unit: GPU processes (fake nvidia-smi){RESET}\n")
    import server

    script = r"""#!/bin/sh
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
    orig_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "nvidia-smi"
        fake.write_text(script)
        fake.chmod(0o755)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{orig_path}"
        try:
            try:
                rows = server._get_gpu_processes()
            except Exception as e:  # the decode bug raised here
                fail("_get_gpu_processes with a non-UTF-8 pmon command name", repr(e))
                return
            got = {(r["gpu_index"], r["pid"]): (r["gpu_memory_mib"], r["gpu_util_percent"], r["gpu_mem_util_percent"])
                   for r in rows}
            check(
                got == {(0, 4242): (100.0, 70.0, 40.0), (1, 4242): (200.0, 20.0, 10.0),
                        (0, 5151): (50.0, None, None)},
                "_get_gpu_processes: compute-apps joined with pmon on (gpu index, pid); "
                "'-' -> None; non-UTF-8 names don't raise",
                repr(got),
            )
            os.environ["FAKE_PMON_SLEEP"] = "5"
            t0 = time.monotonic()
            rows = server._get_gpu_processes()
            elapsed = time.monotonic() - t0
            check(
                len(rows) == 3 and all(r["gpu_util_percent"] is None for r in rows) and elapsed < 4,
                "stalled pmon: process list kept, utilization None, well under the browser's 5 s",
                f"{len(rows)} rows in {elapsed:.1f}s",
            )
        finally:
            os.environ["PATH"] = orig_path
            os.environ.pop("FAKE_PMON_SLEEP", None)


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
    gpu = [{"pid": P + 3, "gpu_index": 1, "gpu_memory_mib": 100.0, "name": "idle", "gpu_util_percent": 40.0},
           {"pid": P + 3, "gpu_index": 0, "gpu_memory_mib": 50.0, "name": "idle", "gpu_util_percent": 2.5},
           {"pid": P + 99, "gpu_index": 0, "gpu_memory_mib": 7.0, "name": "ghost", "gpu_util_percent": None}]
    rows = server.build_process_records(procs, gpu, 2)
    by_pid = {r["pid"]: r for r in rows}
    check(
        [r["pid"] for r in rows] == [P + 1, P + 4, P + 2, P + 3, P + 99]
        and by_pid[P + 1]["name"] == "hog"
        and by_pid[P + 2]["rss_mb"] == 900.0
        and by_pid[P + 3]["gpu_mem_mib"] == 150.0
        and by_pid[P + 3]["gpus"] == [0, 1]
        and by_pid[P + 3].get("gpu_util_percent") == 42.5  # summed over its GPUs
        and "gpu_util_percent" not in by_pid[P + 99]  # pmon had no sample -> no key
        and "gpus" not in by_pid[P + 1]
        and "cpu_percent" not in by_pid[P + 99]
        and by_pid[P + 99]["gpu_mem_mib"] == 7.0
        and all("ppid" not in r and "create_time" not in r for r in rows)  # pids gone -> omitted
        and all(v is not None for r in rows for v in r.values())  # sparse: no nulls
        and json.loads(json.dumps(rows)) == rows,
        "build_process_records: top-2 cpu + top-2 rss + GPU holders, sorted, GPU-only last",
        describe(rows),
    )
    ghosts = {r["pid"]: r for r in server.build_process_records([proc(P + 96, 1.0, 1 << 20, "listed")], [
        {"pid": P + 98, "gpu_index": 0, "gpu_memory_mib": 1.0, "name": "g", "gpu_util_percent": 12.0},
        {"pid": P + 97, "gpu_index": 0, "gpu_memory_mib": 1.0, "name": "z", "gpu_util_percent": 0.0},
        {"pid": P + 96, "gpu_index": 0, "gpu_memory_mib": 1.0, "name": "listed", "gpu_util_percent": 0.0}], 5)}
    check(
        ghosts[P + 98].get("gpu_util_percent") == 12.0
        and "gpu_util_percent" not in ghosts[P + 97] and "gpu_util_percent" not in ghosts[P + 96],
        "build_process_records: GPU-only rows keep utilization; 0 is omitted (listed or GPU-only)",
        describe(list(ghosts.values())),
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
    pm = server._parse_pmon(
        "# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command \n"
        "# Idx           #    C/G      %      %      %      %      %      %    name \n"
        "    0    1498409     C     97     75      -      -      -      -    python3        \n"
        "    0    1498411     C      -      -      -      -      -      -    python3        \n"
        "    1    1498408     C     87     48      -      -      -      -    python3        \n"
        "garbage line\n"
    )
    check(
        pm == {(0, 1498409): (97.0, 75.0), (0, 1498411): (None, None), (1, 1498408): (87.0, 48.0)},
        "_parse_pmon: (gpu, pid) -> (sm, mem), '-' -> None, headers and junk skipped",
        repr(pm),
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


def test_history_process_sources_unit() -> None:
    """A missing Glances process list must not discard available GPU processes."""
    from unittest.mock import Mock, patch
    import server

    print(f"\n{BOLD}History process source recovery unit tests{RESET}\n")
    pid = 2**31 - 1  # cannot be a live process on Linux
    gpu_rows = [{"pid": pid, "name": "trainer", "gpu_index": 0,
                 "gpu_memory_mib": 4096, "gpu_util_percent": 90}]
    cpu_rows = [{"pid": pid, "name": "trainer", "cpu_percent": 20,
                 "memory_info": {"rss": 1024**2}}]
    responses = {
        "cpu": {"total": 20}, "percpu": [{"total": 20}], "network": [], "diskio": [],
        "mem": {"used": 1024, "total": 2048, "percent": 50}, "memswap": {},
        "gpu": [], "sensors": [], "load": {}, "fs": [],
    }
    requests = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            name = url.rsplit("/", 1)[1]
            requests.append(name)
            return process_reply if name == "processlist" else httpx.Response(200, json=responses[name])

    async def run():
        nonlocal process_reply
        with patch.multiple(server.APP.state, create=True,
                            history_sample_duration=1, history_sample_count=1,
                            history_processes_enabled=True, history_process_count=10, ping_enabled=False), \
                patch.object(server.httpx, "AsyncClient", Client), \
                patch.multiple(server, _read_cpu_freqs=Mock(return_value=[]),
                               _get_ip_info=Mock(return_value={}), get_gpu_stats=Mock(return_value=[]),
                               _get_storage_health=Mock(return_value={"status": "missing"})), \
                patch.object(server, "_get_gpu_processes", return_value=gpu_rows) as gpu:
            for label, status, data in (("failed", 500, []), ("empty", 200, []), ("non-list", 200, {})):
                process_reply = httpx.Response(status, json=data)
                gpu.reset_mock()
                record = await server.collect_history_sample("http://fixture")
                rows = record.get("processes", [])
                check(gpu.call_count == 1 and len(rows) == 1
                      and rows[0].get("gpu_mem_mib") == 4096 and rows[0].get("gpu_util_percent") == 90
                      and rows[0].get("gpus") == [0] and "cpu_percent" not in rows[0]
                      and record["cpu"]["avg"] == 20 and record["memory"]["percent"] == 50,
                      f"History: {label} Glances processes preserve GPU rows and other metrics")

            server.APP.state.history_processes_enabled = False
            gpu.reset_mock()
            requests.clear()
            record = await server.collect_history_sample("http://fixture")
            check(gpu.call_count == 0 and "processes" not in record and "processlist" not in requests,
                  "History: disabled process recording skips both process sources")

            server.APP.state.history_processes_enabled = True
            process_reply = httpx.Response(200, json=cpu_rows)
            gpu.side_effect = FileNotFoundError("nvidia-smi")
            record = await server.collect_history_sample("http://fixture")
            rows = record.get("processes", [])
            check(len(rows) == 1 and rows[0].get("cpu_percent") == 20
                  and rows[0].get("rss_mb") == 1 and "gpu_mem_mib" not in rows[0],
                  "History: missing NVIDIA keeps Glances process rows")

    process_reply = None
    asyncio.run(run())


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


def test_text_view_unit() -> None:
    """server.render_text: ascii stays 7-bit, unicode draws bars, empty and gappy input."""
    print(f"\n{BOLD}render_text unit tests{RESET}\n")
    import server

    now = datetime.now(timezone.utc)
    recs = [
        {
            "timestamp": (now - timedelta(minutes=10 * (3 - k))).isoformat(),
            "cpu": {"avg": 10.0 * k, "max": 50.0},
            "memory": {"used_gb": 4.0, "total_gb": 16.0, "percent": 25.0},
            "gpus": [{"index": 0, "util_percent": 80.0, "mem_used_gb": None}],
            "temps": {"GPU 0": 60.0, "Tctl": 45.0},
            "ping": {"8.8.8.8": {"avg": None, "loss": 1.0}},
        }
        for k in range(4)
    ]
    recs[1] = {"timestamp": recs[1]["timestamp"]}  # a record missing every metric
    ascii_out = server.render_text(recs, style="ascii", title="box")
    check(ascii_out.isascii(), "ascii output is 7-bit")
    check("GPU 0" in ascii_out and "  memory" in ascii_out and "DOWN" in ascii_out, "latest shows GPU util + memory and down ping")
    check(ascii_out.count("\n") > 10 and "History" in ascii_out, "history table rendered")
    uni = server.render_text(recs, style="unicode", view="latest", title="box")
    check("█" in uni and "History" not in uni, "unicode latest-only has bars, no history")
    hist = server.render_text(recs, style="ascii", view="history", rows=2, table=True)
    check("Snapshot" not in hist and hist.count(" | ") == 3, "history-only, rows capped", repr(hist.count(" | ")))
    newest_stamp = server._record_time(recs[-1]).strftime("%m-%d %H:%M:%S")
    one = server.render_text(recs, style="ascii", view="history", rows=1, table=True)
    one_rows = [line for line in one.splitlines() if " | " in line][1:]
    check(len(one_rows) == 1 and one_rows[0].startswith(newest_stamp),
          "a one-row history table shows the newest record")
    roomy = server.render_text(recs, style="ascii", view="history", rows=24, table=True)
    roomy_rows = [line for line in roomy.splitlines() if " | " in line][1:]
    check(len(roomy_rows) == len(recs) and roomy_rows[-1].startswith(newest_stamp),
          "a history table with spare rows includes every record")
    check("No history records" in server.render_text([]), "empty history")
    colored = server.render_text(recs, style="unicode", color=True)
    check("\033[38;2;59;130;246m" in colored, "colour mode uses the frontend CPU blue")
    check("\033[" not in uni, "no escapes unless colour is on")
    check(" | " not in server.render_text(recs, style="ascii"), "no table unless table=1")
    wide = server.render_text(recs, width=180)
    check(max(len(server._strip_ansi(x)) for x in wide.splitlines()) <= 180
          and any("Snapshot" in x and "History" in x for x in wide.splitlines()),
          "width=180 puts snapshot and history side by side within the width")
    narrow = server.render_text(recs, width=80)
    check(max(len(x) for x in narrow.splitlines()) <= 80, "width=80 stays within 80 columns")
    check(server._sparkline([72.2, 74.0], lo=0, hi=100) == "▆▆", "percent rows use a fixed 0-100 scale")
    check(server._sparkline([0.0, 0.0], lo=0, hi=100) != server._sparkline([100.0, 100.0], lo=0, hi=100),
          "flat 0% and flat 100% look different")
    check(server.auto_history_hours(80, "both", True, 600) == 3.0
          and server.auto_history_hours(80, "both", True, 60) == 1.0
          and 1.0 < server.auto_history_hours(80, "both", True, 120) < 3.0,
          "auto history window stays in 1-3 h and scales with the record interval")
    # Use the host's local timezone just as the real /text route does. The
    # timezone attached by astimezone() has only the offset at that instant.
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        for utc_end, expected_start in [
            (datetime(2026, 3, 8, 16, tzinfo=timezone.utc), "23:00"),
            (datetime(2026, 11, 1, 17, tzinfo=timezone.utc), "01:00"),
        ]:
            _, _, foot, _ = server._render_history(
                recs, False, 12, 24, False, now=utc_end.astimezone(),
            )
            ticks = foot[1].strip()
            check(ticks.startswith(expected_start) and ticks.endswith("12:00"),
                  f"history time axis crosses DST correctly ({utc_end:%Y-%m-%d})", ticks)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_tui_unit() -> None:
    """tui.py: terminal detection, escape-safe clipping, URL building; server colour depths."""
    print(f"\n{BOLD}tui / colour depth unit tests{RESET}\n")
    import server
    import tui

    tty = {"TERM": "xterm-256color"}
    check(tui.detect(tty, True, "UTF-8") == ("unicode", "256"), "xterm-256color + UTF-8 -> unicode, 256")
    check(tui.detect({**tty, "COLORTERM": "truecolor"}, True, "utf-8")[1] == "on", "COLORTERM truecolor -> 24-bit")
    check(tui.detect({**tty, "NO_COLOR": "1", "COLORTERM": "truecolor"}, True, "UTF-8")[1] == "off", "NO_COLOR wins")
    check(tui.detect({**tty, "FORCE_COLOR": "1"}, True, "UTF-8")[1] == "16", "FORCE_COLOR=1 -> 16")
    check(tui.detect({"TERM": "linux"}, True, "UTF-8") == ("ascii", "16"), "Linux console -> ascii, 16")
    check(tui.detect({"TERM": "dumb"}, True, "ANSI_X3.4-1968") == ("ascii", "off"), "dumb + C locale -> ascii, off")
    check(tui.detect(tty, False, "UTF-8") == ("simple", "off"), "not a tty -> simple")
    red = "\033[31mabcdef\033[0m"
    check(tui.clip(red, 3) == "\033[31mabc\033[0m", "clip keeps escapes whole", repr(tui.clip(red, 3)))
    check(tui.clip("abc", 10) == "abc", "clip leaves short lines alone")
    url = tui.build_url("http://h:8080/", {"view": "both", "hours": None, "width": 120})
    check(url == "http://h:8080/text?view=both&width=120", "build_url drops unset params", url)
    check(server._paint("x", "cpu", "256").startswith("\033[38;5;"), "256-colour depth")
    check(server._paint("x", "muted", "256") == "\033[38;5;244mx\033[0m", "greys use the 256 grey ramp")
    check(server._paint("x", "error", "16").startswith("\033[3") or server._paint("x", "error", "16").startswith("\033[9"),
          "16-colour depth")
    tall = "\n".join(str(i) for i in range(50)) + "\n"
    fit = server._fit_height(tall, 10, False)
    check(fit.count("\n") == 10 and "41 more lines" in fit, "height fit trims and says how much", repr(fit[-40:]))


def test_regression_suites() -> None:
    """Focused suites with their own subprocess/DOM/installer fixtures."""
    print(f"\n{BOLD}Failure and recovery regression suites{RESET}\n")
    for label, command in [
        ("process argument privacy", [sys.executable, "-m", "unittest", "test_process_info"]),
        ("browser timeouts, recovery, freshness and ping loss", ["node", "test_frontend.js"]),
        ("OpenWrt installer recovery", [sys.executable, "openwrt/test_install.py"]),
        ("nginx installer recovery", [sys.executable, "nginx/test_install.py"]),
        ("GPU sensor compatibility and thermal alarms", [sys.executable, "collector/test_sensors.py"]),
    ]:
        if command[0] == "node" and not shutil.which("node") and os.environ.get("HORATIO_SKIP_NODE") == "1":
            print("  SKIP  browser recovery tests (HORATIO_SKIP_NODE=1, node not installed)")
            continue
        try:
            result = subprocess.run(
                command, cwd=Path(__file__).parent, capture_output=True,
                text=True, timeout=60,
            )
            detail = (result.stdout + result.stderr).strip()[-2000:] if result.returncode else ""
            check(result.returncode == 0, label, detail)
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(label, str(exc))


def main() -> int:
    global passes, fails
    test_read_history_unit()
    test_append_history_unit()
    test_history_export_unit()
    test_rate_clamp_unit()
    test_history_fetch_js()
    test_auth_throttle_unit()
    test_subprocess_decoding_unit()
    test_gpu_info_partial_unit()
    test_gpu_stats_compatibility_unit()
    test_system_info_unit()
    test_null_metrics_unit()
    test_process_history_unit()
    test_history_process_sources_unit()
    test_gpu_processes_unit()
    test_dedupe_fs_unit()
    test_mail_and_health_unit()
    test_text_view_unit()
    test_tui_unit()
    test_regression_suites()
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
        # Glances decodes again: double-encoded and ../-prefixed spellings too.
        for path in ("/api/4/x/../processlist", "/api/4/./all", "/api/4/%2e%2e/4/processlist",
                     "/api/4/process%256cist", "/api/4/a%256cl", "/api/%2e%2e/api/4/processlist",
                     "/api/../api/4/processlist", "/api/4/cpu/../processlist", "/api/4/config"):
            try:
                conn = http.client.HTTPConnection(host.hostname, host.port, timeout=10)
                conn.request("GET", path)
                status = conn.getresponse().status
                conn.close()
                check(status == 404, f"GET {path} (raw) is 404", f"status={status}")
            except Exception as e:
                fail(f"GET {path} (raw)", str(e))

        # ── console text view ───────────────────────────────────────
        for path, want_color in (
            ("/text", True),
            ("/text?style=simple", False),
            ("/text?style=unicode", True),
            ("/text?style=unicode&color=off&view=history", False),
        ):
            try:
                r = httpx.get(f"{base}{path}", timeout=10)
                check(
                    r.status_code == 200 and ("\033[" in r.text) == want_color,
                    f"GET {path}",
                    f"status={r.status_code}",
                )
            except Exception as e:
                fail(f"GET {path}", str(e))
        r = httpx.get(f"{base}/text?style=simple", timeout=10)
        check(r.text.isascii(), "GET /text?style=simple is 7-bit")
        r = httpx.get(f"{base}/text?height=12&color=256", timeout=10)
        check(r.status_code == 200 and r.text.count("\n") <= 12 and "\033[38;5;" in r.text,
              "GET /text?height=12&color=256 fits and uses 256 colours")
        r = httpx.get(f"{base}/tui.py", timeout=10)
        check(r.status_code == 200 and "def detect(" in r.text, "GET /tui.py serves the client")
        r = httpx.get(f"{base}/help", timeout=10)
        check(r.status_code == 200 and "/text" in r.text and "/api/history" in r.text, "GET /help")
        r = httpx.get(f"{base}/text?style=fancy", timeout=10)
        check(r.status_code == 400, "GET /text?style=fancy is 400", f"status={r.status_code}")

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

        # ── Incremental history (since=) and gzip ────────────────────
        try:
            h = httpx.get(f"{base}/api/history?hours=24", timeout=30).json()
            check("cutoff" in h, "history response carries cutoff")
            newest = max(
                (r["timestamp"] for r in h.get("records", [])),
                key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
                default=None,
            )
            if newest:
                newest_ts = datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp()
                r = httpx.get(
                    f"{base}/api/history", params={"hours": 24, "since": newest}, timeout=30
                )
                d = r.json()
                # The background initial sample can land between the two reads, so
                # the contract is "strictly newer", not "nothing".
                check(
                    r.status_code == 200
                    and d.get("count") == len(d.get("records", [])) <= 1
                    and all(
                        datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp()
                        > newest_ts
                        for x in d.get("records", [])
                    )
                    and "cutoff" in d,
                    "GET /api/history?since=<newest> returns only strictly newer records",
                    describe(d),
                )
                r = httpx.get(
                    f"{base}/api/history/field/temps",
                    params={"hours": 24, "since": newest},
                    timeout=30,
                )
                check(
                    r.status_code == 200 and "cutoff" in r.json() and r.json().get("count", 99) <= 1,
                    "GET /api/history/field/temps?since= works",
                    describe(r.json()),
                )
            else:
                fail("history since= checks", "no records to take a timestamp from")
            check(
                isinstance(h.get("cutoff"), (int, float))
                and abs(h["cutoff"] - (time.time() - 24 * 3600)) < 120,
                "history cutoff is the window start (now - hours)",
                str(h.get("cutoff")),
            )
            check(
                isinstance(h.get("now"), (int, float)) and abs(h["now"] - time.time()) < 120
                and abs(h["now"] - h["cutoff"] - 24 * 3600) < 1e-6,
                "history now is the server clock (cutoff + hours)",
                str(h.get("now")),
            )
            if newest and "+" in newest:
                # Pasted unencoded, the '+' of '+00:00' arrives as a space.
                r = httpx.get(f"{base}/api/history?hours=24&since={newest}", timeout=30)
                check(r.status_code == 200, "since= with an unencoded '+00:00' is accepted", r.text[:200])
            r = httpx.get(f"{base}/api/history?hours=24&since=garbage", timeout=30)
            check(r.status_code == 400, "GET /api/history?since=garbage -> 400", r.text[:200])
            r = httpx.get(
                f"{base}/static/app.js",
                # Above the 1000-byte gzip minimum, so a missing bypass would show.
                headers={"Accept-Encoding": "gzip", "Range": "bytes=0-4999"},
                timeout=30,
            )
            check(
                r.status_code == 206 and "content-encoding" not in r.headers and len(r.content) == 5000,
                "Range requests are not gzipped (Content-Range stays true)",
                f"{r.status_code} {dict(r.headers)}",
            )
            r = httpx.get(f"{base}/", headers={"Accept-Encoding": "gzip"}, timeout=30)
            check(
                r.status_code == 200 and r.headers.get("content-encoding") == "gzip",
                "responses are gzipped for clients that accept it",
                str(dict(r.headers)),
            )
            r = httpx.get(f"{base}/text", headers={"Accept-Encoding": "identity"}, timeout=30)
            check(
                r.status_code == 200 and "content-encoding" not in r.headers,
                "no gzip without Accept-Encoding: gzip (tui.py path)",
                str(dict(r.headers)),
            )
        except Exception as e:
            fail("history since= / gzip", str(e))

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

            # Served rates obey the configured caps (the box's config.json may
            # set its own; defaults otherwise).
            try:
                cfg = httpx.get(f"{base}/config.json", timeout=10).json()
                lims = cfg.get("limits") if isinstance(cfg.get("limits"), dict) else {}

                def cap_of(key: str, default: float) -> float:
                    # The server's _cfg_num rules: a number in [1, 1e9], else default.
                    v = lims.get(key, default)
                    ok_ = isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 1e9
                    return float(v) if ok_ else default

                dio_cap = cap_of("disk_io_mbps", 15000.0)
                first_hist = (httpx.get(f"{base}/api/history?hours=24", timeout=30).json()
                              .get("records") or [{}])[0]

                def within(stats: object, cap: float) -> bool:
                    return isinstance(stats, dict) and all(
                        v is None or 0 <= v <= cap for v in stats.values()
                    )

                for label, rec_ in (("echo", record), ("history", first_hist)):
                    net = rec_.get("network", {})
                    dio_ = rec_.get("disk_io", {})

                    def nonneg(stats: object) -> bool:
                        return isinstance(stats, dict) and all(v is None or v >= 0 for v in stats.values())

                    # Each drive is capped; totals are uncapped sums (non-negative).
                    check(
                        all(nonneg(net.get(k)) for k in ("in_mbps", "out_mbps") if k in net)
                        and all(nonneg(dio_.get(k)) for k in ("read_mbps", "write_mbps") if k in dio_)
                        and all(
                            within(d.get(k), dio_cap)
                            for d in dio_.get("drives", []) for k in ("read_mbps", "write_mbps") if k in d
                        ),
                        f"{label} record: drives within limits (disk<={dio_cap}), totals non-negative",
                        describe({"network": net, "disk_io": dio_}),
                    )
            except Exception as e:
                fail("served rates within limits", str(e))
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
