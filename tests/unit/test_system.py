from __future__ import annotations

import email.message
import json
import os
import subprocess
import tempfile
import time
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO
from unittest.mock import patch

import httpx
import pytest

from tests.support import describe


def test_subprocess_decoding_unit() -> None:
    """pidstat output with a process name cut mid-character must not raise
    (the Disk I/O Processes card errored on every capture)."""
    import server

    orig_path: str = os.environ.get("PATH", "")
    tmp: str
    with tempfile.TemporaryDirectory() as tmp:
        fake: Path = Path(tmp) / "pidstat"
        fake.write_text(
            "#!/bin/sh\nprintf 'Average:  1000  4242  12.00  3.00  0.00  caf\\303\\n'\n"
        )
        fake.chmod(0o755)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{orig_path}"
        e: Exception
        try:
            out: str = server._run_pidstat(1, 5).stdout
            assert "caf�" in out, (
                f"_run_pidstat: invalid UTF-8 replaced, not raised: {repr(out)}"
            )
        except Exception as e:
            pytest.fail(f"{'_run_pidstat with non-UTF-8 output'}: {repr(e)}")
        finally:
            os.environ["PATH"] = orig_path


def test_system_info_unit() -> None:
    """CPU topology and address-family fallbacks, without hardware or network."""
    from unittest.mock import patch

    import server

    # Core IDs repeat across sockets and SMT siblings; field order is arbitrary.
    multi: str = "\n\n".join(
        f"processor : {i}\ncore id : {(i % 4) // 2}\nphysical id : {i // 4}\n"
        "model name : Fixture CPU\ncache size : 32768 KB"
        for i in range(8)
    )
    no_socket: str = "\n\n".join(
        f"processor : {i}\ncore id : {i // 2}" for i in range(4)
    )
    no_topology: str = "processor : 0\n\nprocessor : 1\n"
    label: str
    cpuinfo: str
    physical: int
    logical: int
    for label, cpuinfo, physical, logical in (
        ("multiple sockets with SMT", multi, 4, 8),
        ("missing socket IDs", no_socket, 2, 4),
        ("missing core IDs", no_topology, 2, 2),
    ):
        with (
            patch.object(server.Path, "exists", return_value=True),
            patch.object(server.Path, "read_text", side_effect=[cpuinfo, "32M\n"]),
        ):
            info: dict[str, Any] = server._get_cpu_info()
        assert (
            info["physical_cores"] == physical
            and info["logical_cores"] == logical
            and (info["l3_cache"] == "32M")
            and (
                label != "multiple sockets with SMT"
                or (info["model"], info["cache_size"]) == ("Fixture CPU", "32768 KB")
            )
        ), f"CPU info: {label}: {describe(info)}"

    first: httpx.ConnectTimeout | str
    second: str | httpx.ConnectTimeout
    expected: dict[str, str]
    for label, first, second, expected in (
        (
            "IPv4 fallback after first provider fails",
            httpx.ConnectTimeout("offline"),
            "198.51.100.9",
            {"ipv4": "198.51.100.9"},
        ),
        (
            "both address families",
            "198.51.100.9",
            "2001:db8::9",
            {"ipv4": "198.51.100.9", "ipv6": "2001:db8::9"},
        ),
        (
            "IPv4-only providers keep the first address",
            "198.51.100.9",
            "198.51.100.10",
            {"ipv4": "198.51.100.9"},
        ),
        (
            "both providers fail",
            httpx.ConnectTimeout("offline"),
            httpx.ConnectTimeout("offline"),
            {"error": "lookup failed"},
        ),
    ):
        client: unittest.mock.MagicMock | unittest.mock.AsyncMock
        with (
            patch.object(
                server.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", ""),
            ),
            patch.object(server.httpx, "Client") as client,
        ):
            client.return_value.__enter__.return_value.get.side_effect = [
                httpx.Response(200, text=value) if isinstance(value, str) else value
                for value in (first, second)
            ]
            info = server._get_ip_info()
        assert info["public"] == expected, (
            f"IP info: {label}: {describe(info['public'])}"
        )


def test_mail_and_health_unit() -> None:
    """Unit tests for the two file readers behind the System Mail and Hardware cards."""
    import base64

    import server

    subject_utf8: str = base64.b64encode("SMART error ✓ <b>&\"'".encode()).decode()
    # postfix-shaped mbox: From_ line, headers, blank line, body, blank separator line.
    # Body lines starting with "From " are quoted as ">From " (mboxo), which is what
    # postfix local(8) writes and what the stdlib mailbox reader relies on.
    fixture: str = (
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
    tmp: str
    with tempfile.TemporaryDirectory() as tmp:
        spool: str = os.path.join(tmp, "spool")
        f: TextIO
        with open(spool, "w", encoding="utf-8") as f:
            f.write(fixture)
        m: dict[str, Any] = server._get_mail(spool)
        assert m["status"] == "ok" and m["count"] == 4, (
            f"mail: fixture spool counts 4: {str(m['count'])}"
        )
        assert [x["subject"] for x in m["messages"]][:3] == [
            "bad date",
            "no date",
            "SMART error ✓ <b>&\"'",
        ], (
            f"mail: newest first, RFC 2047 subject decoded: {str([x['subject'] for x in m['messages']])}"
        )
        assert [x["date_epoch"] is None for x in m["messages"]] == [
            True,
            True,
            False,
            False,
        ], (
            f"mail: garbage or missing Date: gives null epoch, real ones parse: {str([x['date_epoch'] for x in m['messages']])}"
        )
        assert (
            server._get_mail(spool, limit=1)["count"] == 4
            and len(server._get_mail(spool, limit=1)["messages"]) == 1
        ), "mail: limit trims messages but not count"
        assert server._get_mail(os.path.join(tmp, "nope"))["status"] == "missing", (
            "mail: missing spool is 'missing', not an exception"
        )
        assert server._get_mail(tmp)["status"] == "error", (
            "mail: a directory is 'error', not an exception"
        )
        # One unparseable message must neither vanish nor take the card down: it
        # stays in the list as a red "unreadable" row. The stdlib parser is
        # lenient (the bad/missing dates above don't raise), so force a raise.
        real_mfb: Callable[..., email.message.Message] = server.email.message_from_bytes

        def boom(data: bytes | bytearray, *a: Any, **k: Any) -> email.message.Message:
            if b"smartd says hi" in data:  # the SMART-error message's body
                raise ValueError("simulated unparseable message")
            return real_mfb(data, *a, **k)

        with patch.object(server.email, "message_from_bytes", boom):
            mp: dict[str, Any] = server._get_mail(spool)
        bad: list[Any] = [x for x in mp["messages"] if x.get("error")]
        assert (
            mp["status"] == "ok"
            and mp["count"] == 4
            and (mp["unreadable"] == 1)
            and (len(mp["messages"]) == 4)
            and (len(bad) == 1)
            and (bad[0]["error"] == "ValueError")
            and bad[0]["subject"].startswith("unreadable message")
        ), (
            f"mail: an unparseable message is a red row (unreadable=1), the rest still show: {describe(mp)}"
        )
        state: str = os.path.join(tmp, "state.json")
        assert server._get_storage_health(state)["status"] == "missing", (
            "health: missing snapshot is 'missing'"
        )
        with open(state, "w") as f:
            json.dump(
                {"ts": time.time() - 60, "alarms": ["md0: DEGRADED"], "drives": []}, f
            )
        h: dict[str, Any] = server._get_storage_health(state)
        assert (
            h["status"] == "ok"
            and h["alarms"] == ["md0: DEGRADED"]
            and (0 <= h["age_sec"] < 120)
        ), f"health: fresh snapshot passes alarms through with age: {describe(h)}"
        with open(state, "w") as f:
            json.dump({"ts": time.time() - 2 * 3600, "alarms": []}, f)
        assert server._get_storage_health(state)["status"] == "stale", (
            "health: two-hour-old snapshot is 'stale'"
        )
        with open(state, "w") as f:
            json.dump({"ts": float("nan"), "alarms": []}, f)  # json allows NaN
        assert server._get_storage_health(state)["status"] == "error", (
            "health: non-finite ts is 'error', not an exception"
        )
        with open(state, "w") as f:
            f.write("{not json")
        e: dict[str, Any] = server._get_storage_health(state)
        assert e["status"] == "error" and e["detail"] == "JSONDecodeError", (
            f"health: corrupt snapshot is 'error' with the exception name: {describe(e)}"
        )
        with open(state, "w") as f:
            json.dump({"status": "hacked", "ts": time.time()}, f)
        assert server._get_storage_health(state)["status"] == "ok", (
            "health: the file cannot override status"
        )
