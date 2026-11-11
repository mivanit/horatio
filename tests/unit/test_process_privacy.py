"""Process argument privacy checks: uv run pytest tests/unit/test_process_privacy.py.

Uses real /proc data for Python's inline-code mode and pure argv fixtures for
other interpreters, so Ruby, Perl, and PHP do not need to be installed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SECRET: str = "HORATIO_TEST_SECRET_MARKER"


class TestProcessPrivacy:
    def test_script_and_module_positions(
        self: TestProcessPrivacy, subtests: pytest.Subtests
    ) -> None:
        import server

        cases: list[tuple[list[str], str, str]] = [
            (
                ["/usr/bin/python3", "-u", "-m", "http.server", "--token", SECRET],
                "http.server",
                "/usr/bin/python3 -m http.server",
            ),
            (
                ["python3.13", "-I", "-B", "app.py", "-m", SECRET],
                "app.py",
                "python3.13 app.py",
            ),
            (
                ["python", "-W", "ignore", "-X", "dev", "/srv/my app.py", SECRET],
                "my app.py",
                "python /srv/my app.py",
            ),
            (
                ["python", "-Wignore", "-Xdev", "-m", "package.module", SECRET],
                "package.module",
                "python -m package.module",
            ),
            (["python", "--", "-script.py", SECRET], "-script.py", "python -script.py"),
            (["pypy3", "script.py", SECRET], "script.py", "pypy3 script.py"),
            (
                ["node", "--no-warnings", "/srv/app.js", "--eval", SECRET],
                "app.js",
                "node /srv/app.js",
            ),
            (["nodejs", "--", "app.js", SECRET], "app.js", "nodejs app.js"),
            (["ruby3.1", "-w", "/srv/app.rb", SECRET], "app.rb", "ruby3.1 /srv/app.rb"),
            (["perl", "-w", "/srv/app.pl", SECRET], "app.pl", "perl /srv/app.pl"),
            (
                ["php8.2", "-n", "-f", "/srv/app.php", SECRET],
                "app.php",
                "php8.2 /srv/app.php",
            ),
            (["php", "/srv/app.php", SECRET], "app.php", "php /srv/app.php"),
        ]
        argv: list[str]
        name: str
        path: str
        for argv, name, path in cases:
            with subtests.test(argv=argv):
                assert server._sanitize_process_argv(argv) == {
                    "name": name,
                    "path": path,
                }

    def test_inline_code_and_unknown_option_operands_are_private(
        self: TestProcessPrivacy,
        subtests: pytest.Subtests,
    ) -> None:
        import server

        code: str = f'token="{SECRET}"'
        cases: list[list[str]] = [
            ["python", "-c", code],
            ["python3.13", "-u", "-c", code, "-m", SECRET],
            ["python", "-c" + code],
            ["python", "-uc", code],
            ["python", "-", SECRET],
            ["python", "--", "-", SECRET],
            ["python", "--unknown-option", SECRET, "app.py"],
            ["python", "-W", SECRET],
            ["python", "-X", SECRET],
            ["python", "-m"],
            ["python", "-m", code],
            ["python-wrapper", SECRET],
            ["node", "-e", code],
            ["node", "--eval", code],
            ["node", "--eval=" + code],
            ["node", "-p", code],
            ["node", "--print", code],
            ["node", "--require", SECRET, "app.js"],
            ["ruby", "-e", code],
            ["ruby", "-I", SECRET, "app.rb"],
            ["perl", "-e", code],
            ["perl", "-E", code],
            ["perl", "-I" + SECRET, "app.pl"],
            ["php", "-r", code],
            ["php", "-d", "password=" + SECRET, "app.php"],
            ["bash", "-c", code],
        ]
        argv: list[str]
        for argv in cases:
            with subtests.test(argv=argv):
                info: dict[str, str] = server._sanitize_process_argv(argv)
                assert info == {"name": argv[0], "path": argv[0]}
                assert SECRET not in info["name"]
                assert SECRET not in info["path"]

    def test_real_python_inline_code_stays_out_of_live_and_history_names(
        self: TestProcessPrivacy,
        subtests: pytest.Subtests,
    ) -> None:
        # Empty option operands must survive /proc splitting; otherwise -c can
        # be mistaken for the preceding option's operand and expose its body.
        import server

        options: list[str]
        for options in ([], ["-W", ""]):
            with subtests.test(options=options):
                child: subprocess.Popen[str] = subprocess.Popen(
                    [
                        sys.executable,
                        *options,
                        "-c",
                        f'import time; token="{SECRET}"; print("ready", flush=True); time.sleep(30)',
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    assert child.stdout is not None
                    # /proc can briefly expose an empty cmdline during exec.
                    # Inspect a running interpreter, not that startup window.
                    assert child.stdout.readline() == "ready\n"
                    info: dict[str, str] = server.get_sanitized_process_info(child.pid)
                    assert SECRET not in info["name"]
                    assert SECRET not in info["path"]
                    assert info["path"] == sys.executable
                    rows: list[dict[str, Any]] = server.build_process_records(
                        [
                            {
                                "pid": child.pid,
                                "name": "python",
                                "cpu_percent": 1,
                                "memory_info": {"rss": 1024},
                            }
                        ],
                        [],
                        1,
                    )
                    assert len(rows) == 1
                    assert rows[0]["name"] == info["name"]
                    assert SECRET not in rows[0]["name"]
                finally:
                    child.terminate()
                    child.wait(timeout=5)
                    if child.stdout is not None:
                        child.stdout.close()

    def test_nethogs_fallback_keeps_only_the_executable(
        self: TestProcessPrivacy,
    ) -> None:
        # nethogs -l prints the whole command line as the program; a process that
        # exited before /proc is read must not fall back to its arguments.
        import server

        gone: int = 2**22 + 1  # above the kernel's pid_max ceiling: never in /proc
        stdout: str = (
            f"/usr/bin/curl -H Authorization:{SECRET} https://x/{gone}/1000\t1.5\t2.5\n"
            "unknown TCP/0/0\t0.5\t0.5\n"
        )
        with patch.object(
            server,
            "_run_nethogs",
            lambda cycles, timeout: subprocess.CompletedProcess([], 0, stdout, ""),
        ):
            resp: server.Response = asyncio.run(server.network_processes(1))
        body: bytes | memoryview = resp.body
        assert SECRET not in bytes(body).decode()
        rows: dict[int, dict[str, Any]] = {r["pid"]: r for r in json.loads(bytes(body))}
        assert (rows[gone]["name"], rows[gone]["path"]) == ("curl", "/usr/bin/curl")
        assert rows[0]["name"] == "unattributed"

    def test_cron_mail_subject_keeps_only_the_program(
        self: TestProcessPrivacy, subtests: pytest.Subtests, tmp_path: Path
    ) -> None:
        import server

        spool: Path = tmp_path / "spool"
        spool.write_text(
            "From root@box  Mon Sep 14 08:00:00 2026\nFrom: root <root@box>\n"
            f"Subject: Cron <root@box> /usr/bin/sync --key {SECRET}\n\nbody\n\n"
        )
        assert [m["subject"] for m in server._get_mail(str(spool))["messages"]] == [
            "Cron <root@box> sync"
        ]

        cases: list[tuple[str, str]] = [
            (
                f"Cron <root@box> /usr/local/bin/backup --password {SECRET}",
                "Cron <root@box> backup",
            ),
            (
                f"Cron <miv@box> TOKEN={SECRET} python3 /srv/sync.py -k {SECRET}",
                "Cron <miv@box> sync.py",
            ),
            (
                f"Cron <root@box> python3 -c 'print(\"{SECRET}\")'",
                "Cron <root@box> python3",
            ),
            (f"Cron <root@box> cd /srv && ./run {SECRET}", "Cron <root@box> cd"),
            (f"Cron <root@box> A={SECRET}", "Cron <root@box>"),
            ("SMART error (Health) on /dev/sda", "SMART error (Health) on /dev/sda"),
        ]
        subject: str
        want: str
        for subject, want in cases:
            with subtests.test(subject=subject):
                assert server._redact_cron_subject(subject) == want
