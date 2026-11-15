from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest.mock
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO
from unittest.mock import patch

import h11
import httpx
from starlette.requests import Request
from starlette.types import Message, Scope

from tests.support import describe, history_line


def test_history_settings_preserve_recording_schedule() -> None:
    """Only an interval change should interrupt the automatic recorder's wait."""
    import server

    server.PASSWORD_FILE.write_text(server._hash_password("test-password"))

    async def save(**settings: int | bool) -> server.Response:
        body: bytes = json.dumps({"password": "test-password", **settings}).encode()

        async def receive() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        request: Request = Request(
            {"type": "http", "client": ("127.0.0.1", 12345)}, receive
        )
        return await server.update_history_settings(request)

    async def run() -> None:
        state: Any = server.APP.state
        state.history_record_interval = 600
        state.history_sample_duration = 10
        state.history_sample_count = 5
        state.manual_record_min_interval = 60
        state.history_process_count = 10
        state.history_processes_enabled = False
        state.ping_enabled = True
        original: asyncio.Task[None] = asyncio.create_task(server.history_recorder())
        state.history_task = original
        try:
            await asyncio.sleep(0)  # Let the actual recorder start its wait.
            response: server.Response = await save(
                min_record_interval=30,
                sample_duration=12,
                sample_count=3,
                process_count=5,
                processes_enabled=True,
                ping_enabled=False,
            )
            assert response.status_code == 200
            assert state.history_task is original and not original.done(), (
                "Changing sampling options or the manual cooldown must preserve the pending recording"
            )
            response = await save(record_interval=600)
            assert response.status_code == 200 and state.history_task is original
            response = await save(record_interval=60)
            assert response.status_code == 200 and state.history_record_interval == 60
            assert original.cancelled() and state.history_task is not original, (
                "Changing the automatic interval must reschedule the recorder"
            )
        finally:
            original.cancel()
            state.history_task.cancel()
            await asyncio.gather(original, state.history_task, return_exceptions=True)

    asyncio.run(run())


def test_history_settings_rejects_non_string_password() -> None:
    """A non-string password is a 400 before the throttle: not a 500 from the
    KDF, and not a strike towards the client's lockout."""
    import server

    server.PASSWORD_FILE.write_text(server._hash_password("test-password"))
    server._auth_failures.clear()

    async def save(password: object) -> server.Response:
        body: bytes = json.dumps({"password": password}).encode()

        async def receive() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        request: Request = Request(
            {"type": "http", "client": ("127.0.0.1", 12345)}, receive
        )
        return await server.update_history_settings(request)

    bad: object
    for bad in (5, None, ["x"], {"a": 1}):
        response: server.Response = asyncio.run(save(bad))
        assert response.status_code == 400, (
            f"password {bad!r}: expected 400, got {response.status_code}"
        )
    assert not server._auth_failures, (
        f"malformed bodies must not count as failed guesses: {server._auth_failures}"
    )


def test_history_burst_pacing() -> None:
    """The burst lasts about sample_duration even with a dead ping target: each
    ping runs alongside the wait for the next sample, which is paced from the
    burst's start. Scaled down 10x from the default 10 s / 5 samples / -W 2."""
    import server

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    class FakeClient(httpx.AsyncClient):
        def __init__(self: FakeClient, **k: Any) -> None:
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(**k)

    def burst(ping_sec: float, latency: float | None) -> tuple[float, dict[str, Any]]:
        def fake_ping(target: str, timeout: int = 2) -> float | None:
            time.sleep(ping_sec)
            return latency

        with (
            patch.object(server.httpx, "AsyncClient", FakeClient),
            patch.object(server, "get_gpu_stats", return_value=[]),
            patch.object(server, "_get_ip_info", return_value={}),
            patch.object(server, "_run_ping", fake_ping),
            patch.object(server, "PING_TARGETS", ["192.0.2.1"]),
        ):
            state: Any = server.APP.state
            state.history_sample_duration = 1
            state.history_sample_count = 5
            state.history_processes_enabled = False
            state.history_process_count = 10
            state.ping_enabled = True
            t0: float = time.monotonic()
            record: dict[str, Any] = asyncio.run(
                server.collect_history_sample("http://glances.invalid")
            )
            return time.monotonic() - t0, record["ping"]["192.0.2.1"]

    # Serial ping-then-sleep took 4 * 0.2 + 5 * 0.2 = 1.8 s here (18 s real).
    elapsed: float
    ping: dict[str, Any]
    elapsed, ping = burst(0.2, None)
    assert 0.9 <= elapsed < 1.4 and ping["loss"] == 1.0, (
        f"dead target: burst ~1 s, every attempt a loss: {elapsed:.2f} s {ping}"
    )
    elapsed, ping = burst(0.01, 5.0)
    assert 0.75 <= elapsed < 1.1 and ping == {
        "avg": 5.0,
        "min": 5.0,
        "max": 5.0,
        "loss": 0.0,
    }, f"healthy target: 5 samples, ~0.8 s: {elapsed:.2f} s {ping}"


def test_read_history_unit() -> None:
    """Unit tests for server._read_history tail-reading (no server process needed)."""
    import server

    orig_history_file: Path = server.HISTORY_FILE
    try:
        tmp: str
        with tempfile.TemporaryDirectory() as tmp:
            hist: Path = Path(tmp) / "history.jsonl"
            server.HISTORY_FILE = hist

            def read(
                hours: float, initial: int = 1024 * 1024, since: float | None = None
            ) -> tuple[list[dict[str, Any]], int]:
                result: tuple[list[dict[str, Any]], int, bool, float] | str = (
                    server._read_history(hours, initial_read_size=initial, since=since)
                )
                if isinstance(result, str):
                    raise AssertionError(f"_read_history failed: {result}")
                records: list[dict[str, Any]]
                skipped: int
                _truncated: bool
                _cutoff: float
                records, skipped, _truncated, _cutoff = result
                return records, skipped

            # 1. Missing, empty, and newline-only files
            assert read(24) == ([], 0), "missing file -> ([], 0)"
            hist.write_text("")
            assert read(24) == ([], 0), "empty file -> ([], 0)"
            hist.write_text("\n")
            assert read(24) == ([], 0), "newline-only file -> ([], 0)"

            # 2. Old + in-range records, single read
            hist.write_text(
                "".join(history_line(100 - i) for i in range(10))  # 100h..91h old
                + "".join(history_line(5 - i) for i in range(5))  # 5h..1h old
            )
            records: list[dict[str, Any]]
            skipped: int
            records, skipped = read(24)
            assert len(records) == 5 and skipped == 0, (
                f"old + in-range -> only in-range, none skipped: {describe(records)}"
            )
            timestamps: list[Any] = [r["timestamp"] for r in records]
            assert timestamps == sorted(timestamps), "records are chronological"

            # 3. Doubling: in-range records extend beyond the initial window
            hist.write_text("".join(history_line(20 - i, pad=100) for i in range(20)))
            records, skipped = read(24, initial=256)
            assert len(records) == 20 and skipped == 0, (
                f"window doubles until range is covered: {describe(records)}"
            )

            # 4. An unterminated last line is a record still being appended: left
            #    for the next read, not counted as corrupt.
            hist.write_text(history_line(30) + history_line(2) + '{"timestamp": "2026-')
            records, skipped = read(24)
            assert len(records) == 1 and skipped == 0, (
                f"unterminated last line left for the next read, not counted: {describe(records)}"
            )

            # 5. Malformed lines mid-file are skipped and counted, neighbors kept
            #    (including valid JSON whose timestamp is not a string)
            hist.write_text(
                history_line(3)
                + "not json\n"
                + "42\n"
                + '{"timestamp": null}\n'
                + '{"timestamp": 5}\n'
                + history_line(2)
            )
            records, skipped = read(24)
            assert len(records) == 2 and skipped == 4, (
                f"malformed mid-file lines (incl. non-string timestamps) skipped and counted: {describe(records)}"
            )

            # 6. A far-past record between in-range ones is excluded, not truncating
            hist.write_text(
                history_line(4)
                + history_line(3)
                + history_line(90000)  # bogus ancient timestamp mid-tail
                + history_line(2)
                + history_line(1)
            )
            records, skipped = read(24)
            assert len(records) == 4 and skipped == 0, (
                f"ancient mid-tail record excluded without truncation: {describe(records)}"
            )

            # 7. Record larger than the initial window (no newline in window)
            hist.write_text(history_line(30) + history_line(1, pad=2000))
            records, skipped = read(24, initial=256)
            assert (
                len(records) == 1
                and len(records[0].get("pad", "")) == 2000
                and (skipped == 0)
            ), f"record larger than window parsed after growth: {describe(records)}"

            # 8. Speed: large file of old records, small in-range tail
            f: TextIO
            with open(hist, "w") as f:
                f.writelines(history_line(200, pad=400) for _ in range(100_000))
                f.writelines(history_line(23.5 * (144 - i) / 144) for i in range(144))
            file_mb: float = hist.stat().st_size / 1e6
            t0: float = time.monotonic()
            records, skipped = read(24)
            elapsed: float = time.monotonic() - t0
            assert len(records) == 144 and skipped == 0, (
                f"large-file tail read returns in-range records: {describe(records)}"
            )
            assert elapsed < 0.5, (
                f"large-file tail read is fast: {elapsed * 1000:.1f}ms for {file_mb:.0f}MB file"
            )

            # 9. Byte cap: a range wider than what fits in max_bytes is truncated,
            #    not read to the start of a huge file.
            hist.write_text(
                "".join(history_line(i * 0.001, pad=500) for i in range(2000))
            )
            result: tuple[list[dict[str, Any]], int, bool, float] | str = (
                server._read_history(24, initial_read_size=4096, max_bytes=64 * 1024)
            )
            assert not isinstance(result, str), result
            truncated: bool
            records, skipped, truncated, _ = result
            assert truncated and len(records) > 0, (
                f"byte cap trips truncated=True with partial recent records: {len(records)} records, truncated={truncated}"
            )
            # Within-cap read of the same file is not truncated.
            result = server._read_history(24, initial_read_size=1024 * 1024)
            assert not isinstance(result, str), result
            truncated_full: bool
            _, _, truncated_full, _ = result
            assert not truncated_full, "within-cap read is not truncated"

            # 10. cutoff: the window start as epoch seconds, also for a missing file.
            hist.unlink()
            missing: tuple[list[dict[str, Any]], int, bool, float] | str = (
                server._read_history(24)
            )
            now: float = datetime.now(timezone.utc).timestamp()
            assert (
                isinstance(missing, tuple)
                and missing[:3] == ([], 0, False)
                and (abs(missing[3] - (now - 24 * 3600)) < 2)
            ), (
                f"missing file -> ([], 0, False, cutoff) with cutoff = now - hours: {describe(missing)}"
            )

            # 11. since: only records strictly newer than it, covered by the first
            #     window even when the range would need several doublings.
            hist.write_text("".join(history_line(20 - i, pad=100) for i in range(20)))
            full: list[dict[str, Any]]
            full, _ = read(24, initial=256)

            def ts_of(r: dict[str, Any]) -> float:
                return datetime.fromisoformat(r["timestamp"]).timestamp()

            second_newest: float = ts_of(full[-2])
            newer: list[dict[str, Any]]
            newer, skipped = read(24, initial=256, since=second_newest)
            assert [r["timestamp"] for r in newer] == [
                full[-1]["timestamp"]
            ] and skipped == 0, (
                f"since=2nd-newest -> only the newest record: {describe(newer)}"
            )
            assert read(24, since=ts_of(full[-1])) == ([], 0), (
                "since=newest -> no records"
            )
            assert read(24, since=ts_of(full[-1]) + 3600) == ([], 0), (
                "since in the future -> no records"
            )
            older: list[dict[str, Any]]
            older, _ = read(24, initial=256, since=now - 48 * 3600)
            assert [r["timestamp"] for r in older] == [r["timestamp"] for r in full], (
                "since older than the cutoff behaves like no since"
            )
            # The record exactly at `since` is held by the client: excluded.
            exact: list[dict[str, Any]]
            exact, _ = read(24, since=ts_of(full[-3]))
            assert len(exact) == 2, (
                f"record at exactly since is excluded: {describe(exact)}"
            )

            # 12. Reaching `since` covers the read: a byte cap that cannot reach
            #     the 24 h cutoff is not hit (truncated stays False) when the
            #     window already holds a record at or before `since`.
            res: tuple[list[dict[str, Any]], int, bool, float] | str = (
                server._read_history(
                    24, initial_read_size=256, max_bytes=600, since=second_newest
                )
            )
            assert not isinstance(res, str), res
            assert [r["timestamp"] for r in res[0]] == [full[-1]["timestamp"]] and res[
                2
            ] is False, (
                f"since stops the tail read early (no truncation under a small byte cap): records={len(res[0])} truncated={res[2]}"
            )
            res = server._read_history(24, initial_read_size=256, max_bytes=600)
            assert not isinstance(res, str), res
            assert res[2] is True, "the same byte cap without since does truncate"
            # Boundary: the window's first whole line is exactly the `since` record.
            lines: list[bytes] = hist.read_bytes().splitlines(keepends=True)
            window: int = (
                len(lines[-1]) + len(lines[-2]) + 10
            )  # 10 bytes of the line before
            res = server._read_history(
                24, initial_read_size=window, max_bytes=window, since=second_newest
            )
            assert not isinstance(res, str), res
            assert len(res[0]) == 1 and res[2] is False, (
                f"a window starting exactly at the since record is covered: records={len(res[0])} truncated={res[2]}"
            )

            # 13. cutoff on a normal read is the window start.
            res = server._read_history(24)
            assert not isinstance(res, str), res
            now = datetime.now(timezone.utc).timestamp()
            assert abs(res[3] - (now - 24 * 3600)) < 2, (
                f"cutoff = now - hours on a non-empty file: cutoff={res[3]} expected~{now - 24 * 3600}"
            )
    finally:
        server.HISTORY_FILE = orig_history_file


def test_append_history_unit(app_dir: Path) -> None:
    """Appending after an interrupted write must preserve old and new records."""
    from unittest.mock import mock_open

    import server

    tmp: str
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(server, "LOGS_DIR", Path(tmp)),
        patch.object(server, "HISTORY_FILE", Path(tmp) / "history.jsonl"),
    ):
        hist: Path = server.HISTORY_FILE
        before: bytes = history_line(2, id="before").encode()
        complete: bytes = history_line(1, id="complete").encode().rstrip(b"\n")
        recovered: dict[str, Any] = json.loads(history_line(0.1, id="recovered"))
        following: dict[str, Any] = json.loads(history_line(0, id="following"))

        def readable_ids() -> tuple[list[str], int]:
            result: tuple[list[dict[str, Any]], int, bool, float] | str = (
                server._read_history(24)
            )
            assert isinstance(result, tuple), result
            return [row["id"] for row in result[0]], result[1]

        label: str
        original: None | bytes
        expected: list[str]
        skipped: int
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
            prefix: bytes = original or b""
            separator: bytes = b"\n" if prefix and not prefix.endswith(b"\n") else b""
            suffix: bytes = b"".join(
                (json.dumps(row) + "\n").encode() for row in (recovered, following)
            )
            assert hist.read_bytes() == prefix + separator + suffix, (
                f"{label}: append preserves every existing byte"
            )
            assert readable_ids() == (expected + ["recovered", "following"], skipped), (
                f"{label}: both acknowledged appends remain readable"
            )

        # A real short write, isolated in a child so the test runner's files and
        # signal disposition are unaffected. Repeat it to cover failed recovery.
        hist.write_bytes(before)
        short_write: str = """
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
"""
        attempt: int
        for attempt in range(2):
            prior: bytes = hist.read_bytes()
            result: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, "-c", short_write, tmp],
                cwd=app_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            partial: bytes = hist.read_bytes()
            assert (
                result.returncode == 0
                and partial.startswith(prior)
                and (len(partial) == len(prior) + 80)
                and (not partial.endswith(b"\n"))
            ), (
                f"short write {attempt + 1}: real failure preserves the prior bytes: {result.stderr}"
            )
        server.append_history_record(recovered)
        server.append_history_record(following)
        assert hist.read_bytes().startswith(partial + b"\n") and readable_ids() == (
            ["before", "recovered", "following"],
            2,
        ), "repeated short writes: recovery keeps old and both subsequent records"

        failure: str
        for failure in ("seek", "read", "short-read"):
            prior = hist.read_bytes()
            opener: unittest.mock.MagicMock = mock_open()
            handle: unittest.mock.MagicMock = opener.return_value
            handle.seek.return_value = len(prior)
            if failure == "short-read":
                handle.read.return_value = b""
            else:
                getattr(handle, failure).side_effect = OSError(
                    "injected tail inspection failure"
                )
            rejected: bool = False
            with patch("server.open", opener, create=True):
                try:
                    server.append_history_record(recovered)
                except OSError:
                    rejected = True
            assert (
                rejected and (not handle.write.called) and (hist.read_bytes() == prior)
            ), f"tail {failure} failure refuses to append or alter history"

        prior = hist.read_bytes()
        rejected = False
        try:
            server.append_history_record({"invalid": object()})
        except TypeError:
            rejected = True
        assert rejected and hist.read_bytes() == prior, (
            "serialization failure leaves history untouched"
        )


def test_history_export_unit() -> None:
    """A concurrent append must not change an export's bytes or HTTP framing."""
    import gzip

    import h11

    import server

    tmp: str
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(server, "LOGS_DIR", Path(tmp)),
        patch.object(server, "HISTORY_FILE", Path(tmp) / "history.jsonl"),
        patch.object(server, "PASSWORD_FILE", Path(tmp) / "password"),
    ):
        server.PASSWORD_FILE.write_text(server._hash_password("export-test"))
        original: bytes = (
            history_line(1, id="existing").encode() * 3000 + b'{"partial":"\xe2\x82'
        )
        appended: dict[str, str] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "id": "new",
        }

        async def export(
            label: str,
            data: bytes,
            *,
            method: Literal["GET", "HEAD"] = "GET",
            headers: dict[str, str] | None = None,
            expected: bytes | None = None,
            status: int = 200,
        ) -> None:
            server.HISTORY_FILE.write_bytes(data)
            scope: Scope = {
                "type": "http",
                "method": method,
                "path": "/api/history/raw",
                "headers": [(b"x-dashboard-password", b"export-test")],
                "query_string": b"",
                "client": ("127.0.0.1", 12345),
                "extensions": {},
            }
            response: server.Response = await server.get_history_raw(Request(scope))
            key: str
            value: str
            for key, value in (headers or {}).items():
                if key == "if-range" and value == "match":
                    value = response.headers["etag"]
                scope["headers"].append((key.encode(), value.encode()))
            connection: h11.Connection = h11.Connection(h11.SERVER)
            connection.receive_data(
                f"{method} /api/history/raw HTTP/1.1\r\nHost: local\r\n\r\n".encode()
            )
            connection.next_event()
            connection.next_event()
            body: list[bytes]
            response_headers: dict[bytes, bytes]
            response_status: int | None
            wrote: bool
            body, response_headers, response_status, wrote = [], {}, None, False

            async def send(message: Message) -> None:
                nonlocal response_headers, response_status, wrote
                if message["type"] == "http.response.start":
                    response_headers = dict(message["headers"])
                    response_status = message["status"]
                    connection.send(
                        h11.Response(
                            status_code=response_status, headers=message["headers"]
                        )
                    )
                else:
                    chunk: bytes = message.get("body", b"")
                    if chunk and not wrote:
                        # Append from a real worker while the download is active.
                        # This also proves the download does not hold the writer lock.
                        await asyncio.wait_for(
                            asyncio.to_thread(server.append_history_record, appended), 2
                        )
                        wrote = True
                    body.append(chunk)
                    connection.send(h11.Data(data=chunk))
                    if not message.get("more_body", False):
                        connection.send(h11.EndOfMessage())

            async def receive() -> Message:
                return {"type": "http.request", "body": b"", "more_body": False}

            await server._GZipUnlessRange(response, minimum_size=1000, compresslevel=1)(
                scope, receive, send
            )
            downloaded: bytes = b"".join(body)
            if response_headers.get(b"content-encoding") == b"gzip":
                downloaded = gzip.decompress(downloaded)
            wanted: bytes = (
                b"" if method == "HEAD" else data if expected is None else expected
            )
            separator: bytes = b"\n" if data and not data.endswith(b"\n") else b""
            disk: bytes = data + (
                separator + (json.dumps(appended) + "\n").encode() if wrote else b""
            )
            assert (
                response_status == status
                and downloaded == wanted
                and (server.HISTORY_FILE.read_bytes() == disk)
            ), f"{label}: exact export, valid HTTP framing, history bytes preserved"

        async def run() -> None:
            await export("full export with torn UTF-8 tail", original)
            await export("gzip export", original, headers={"accept-encoding": "gzip"})
            await export(
                "matching If-Range",
                original,
                headers={"range": "bytes=10-100000", "if-range": "match"},
                expected=original[10:100001],
                status=206,
            )
            await export(
                "stale If-Range falls back to full export",
                original,
                headers={"range": "bytes=10-100000", "if-range": '"old"'},
            )
            await export("empty file", b"")
            await export("exact chunk boundary", b"x" * server.FileResponse.chunk_size)
            await export(
                "small file with gzip accepted",
                b"{}\n",
                headers={"accept-encoding": "gzip"},
            )
            # Test the response's existing HEAD behavior without changing the GET-only route.
            await export("HEAD response", original, method="HEAD")

        asyncio.run(run())


def test_history_max_hours_clamps_every_history_read() -> None:
    """history.max_hours bounds the window /api/history and its side fields read;
    a larger request is served as the largest allowed window, not refused."""
    import server

    assert server.HISTORY_MAX_HOURS == 8760, "config.example.json keeps the old cap"
    asked: list[float] = []

    def fake_read(
        hours: float, since: float | None = None
    ) -> tuple[list[dict[str, Any]], int, bool, float]:
        asked.append(hours)
        return [], 0, False, 0.0

    with (
        patch.object(server, "HISTORY_MAX_HOURS", 48.0),
        patch.object(server, "_read_history_cached", fake_read),
    ):
        assert server._clamp_hours(8760) == 48.0
        assert server._clamp_hours(0) == 0.1
        assert server._clamp_hours(5) == 5
        asyncio.run(server.get_history(hours=8760))
        asyncio.run(server.get_history_field("temps", hours=1000))
    assert asked == [48.0, 48.0], f"history reads use the clamped window: {asked}"


def test_history_window_cache_unit() -> None:
    """One parse of the history file answers every reader until the file changes:
    full reads, since= top-ups and side fields, without changing the shared
    records; each doubling of the window reads only the bytes it did not hold."""
    import server

    now: float = time.time()

    def line(age_s: float, pad: int = 0) -> str:
        stamp: str = datetime.fromtimestamp(now - age_s, timezone.utc).isoformat()
        return json.dumps(
            {"timestamp": stamp, "ip": {"lan": []}, "temps": {"a": 1}, "pad": "x" * pad}
        )

    server.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # 40 records, 10 minutes apart, ~1 KiB each: the newest is 5 minutes old.
    lines: list[str] = [line(600 * (40 - i) - 300, 1000) for i in range(40)]
    server.HISTORY_FILE.write_text("\n".join(lines) + "\n")

    parses: list[int] = []
    real_parse: Callable[..., Any] = server._parse_history_lines

    def counting_parse(data: bytes) -> Any:
        parses.append(len(data))
        return real_parse(data)

    with patch.object(server, "_parse_history_lines", counting_parse):
        # Growth: an 8 KiB window doubled until it holds 5 hours parses each byte once.
        window: Any = server._read_history_window(now - 5 * 3600, None, 8192)
        assert len(parses) > 1, "the window had to grow"
        assert sum(parses) <= window.size, (
            f"bytes parsed once: {parses} of {window.size}"
        )
        assert [ts for ts, _ in window.records] == sorted(
            ts for ts, _ in window.records
        )
        fresh: Any = server._read_history(5, initial_read_size=8192)
        assert len(fresh[0]) == 30 and fresh[1:3] == (0, False)

        parses.clear()
        full: Any = server._read_history_cached(2)
        assert len(full[0]) == 12 and len(parses) == 1, "the first read parses"
        again: Any = server._read_history_cached(2)
        top_up: Any = server._read_history_cached(2, since=now - 1200)
        wider: Any = server._read_history_cached(6)
        assert len(parses) == 1, "later reads the window covers parse nothing"
        assert len(again[0]) == 12 and len(wider[0]) == 36
        assert [r["timestamp"] for r in top_up[0]] == [
            r["timestamp"] for r in full[0][-2:]
        ], "since= returns only the newer records"

        # The routes leave fields out of copies: the shared records keep them.
        body: dict[str, Any] = json.loads(
            bytes(asyncio.run(server.get_history(hours=2)).body)
        )
        assert body["count"] == 12 and "ip" not in body["records"][0]
        assert "temps" not in body["records"][0] and "pad" in body["records"][0]
        field: dict[str, Any] = json.loads(
            bytes(asyncio.run(server.get_history_field("temps", hours=2)).body)
        )
        assert field["records"][0]["temps"] == {"a": 1}, (
            "the field survived /api/history"
        )
        assert "ip" in full[0][0] and len(parses) == 1, "routes read the same window"

        # An appended record invalidates the window.
        with open(server.HISTORY_FILE, "a") as f:
            f.write(line(0) + "\n")
        os.utime(server.HISTORY_FILE, ns=(time.time_ns(), time.time_ns()))
        assert len(server._read_history_cached(2)[0]) == 13 and len(parses) == 2

        # A window over the cache limit is served but not kept.
        with patch.object(server, "HISTORY_CACHE_MAX_WINDOW", 1024):
            assert len(server._read_history_cached(2)[0]) == 13 and len(parses) == 2
            os.utime(server.HISTORY_FILE, ns=(1, 1))
            assert len(server._read_history_cached(2)[0]) == 13 and len(parses) == 3
            assert server._history_cache["window"] is None
            assert len(server._read_history_cached(2)[0]) == 13 and len(parses) == 4
