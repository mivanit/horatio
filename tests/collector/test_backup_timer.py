#!/usr/bin/env python3
"""Run the collector's real backup-timer capture against a fake systemctl."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from tests.support import ROOT

SOURCE: str = (ROOT / "collector/health-collect.sh").read_text()
# From truncating the sidecars to the end of the `if [ -n "$BACKUP_TIMER" ]` block.
START: int = SOURCE.index(': > "$tmp/backup-timer.txt"')
CAPTURE: str = SOURCE[START : SOURCE.index("\nfi\n", START) + len("\nfi\n")]


def capture(
    reply: str, timer: str | None = "system-backup.timer", load: str = "loaded"
) -> tuple[str, str, list[str]]:
    """Return (backup-timer.txt, backup-timer.err, systemctl argv) for one run
    where `systemctl show` prints `reply`."""
    folder: str
    with tempfile.TemporaryDirectory(prefix="horatio-backup-test-") as folder:
        stage: Path = Path(folder)
        (stage / "config.json").write_text(json.dumps({"backup_timer": timer}))
        fake_bin: Path = stage / "bin"
        fake_bin.mkdir()
        systemctl: Path = fake_bin / "systemctl"
        systemctl.write_text(
            f'#!/bin/bash\necho "$@" > "{stage}/argv"\n'
            'case "$*" in *LoadState*) printf "%s\\n" "$FAKE_LOAD" ;; '
            '*) printf "%s\\n" "$FAKE_REPLY" ;; esac\n'
        )
        systemctl.chmod(0o755)
        env: dict[str, str] = dict(
            os.environ,
            PATH=f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            HEALTH_CONFIG=str(stage / "config.json"),
            FAKE_REPLY=reply,
            FAKE_LOAD=load,
            TZ="UTC",
        )
        subprocess.run(
            [
                "bash",
                "-c",
                'set -uo pipefail\ntmp="$1"\n' + CAPTURE,
                "bash",
                str(stage),
            ],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
        argv_file: Path = stage / "argv"
        argv: list[str] = argv_file.read_text().split() if argv_file.exists() else []
        return (
            (stage / "backup-timer.txt").read_text().strip(),
            (stage / "backup-timer.err").read_text().strip(),
            argv,
        )


def test_unix_timestamp_is_epoch_seconds() -> None:
    last: str
    err: str
    argv: list[str]
    last, err, argv = capture("@1794910001")
    assert (last, err) == ("1794910001", "")
    assert argv[:2] == ["show", "system-backup.timer"]


def test_date_from_systemd_that_ignores_the_unix_flag_is_epoch_seconds() -> None:
    # systemd 255 prints this for `show --timestamp=unix`; the python stage
    # used to crash on int() of it and never wrote state.json.
    assert capture("Tue 2026-11-17 03:10:05 UTC")[:2] == ("1794885005", "")


def test_never_fired_stays_empty_without_an_error() -> None:
    assert capture("")[:2] == ("", "")


def test_a_timer_that_does_not_exist_is_an_error() -> None:
    # `systemctl show` on a missing unit prints an empty value and exits 0.
    assert capture("", load="not-found")[:2] == (
        "",
        "no such timer: system-backup.timer",
    )


def test_unparsable_value_is_an_error_not_a_number() -> None:
    last: str
    err: str
    last, err, _ = capture("not a time")
    assert last == ""
    assert "exit" in err


def test_no_timer_configured_does_not_ask_systemctl() -> None:
    assert capture("@1794910001", timer=None) == ("", "", [])
