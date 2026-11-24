"""Collector lifecycle against temporary paths and mocked systemd commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT

MOCK = r"""
import json, os, pathlib, subprocess, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["COLLECTOR_TEST"])
with (root / "events").open("a") as out:
    out.write(json.dumps({"argv": [name, *args], "disabled":
        (root / "config/collectors-disabled").exists()}) + "\n")
if name == "id":
    print("0")
elif name == "systemctl":
    sys.exit(1 if args[0] == os.environ.get("SYSTEMCTL_FAIL") else 0)
elif name == "install":
    # Keep real file installation/modes; omit privileged ownership changes.
    kept = []
    while args:
        arg = args.pop(0)
        if arg in ("--owner", "--group"):
            args.pop(0)
        else:
            kept.append(arg)
    sys.exit(subprocess.call([os.environ["REAL_INSTALL"], *kept]))
"""


class Installation:
    def __init__(self: Installation, root: Path) -> None:
        self.root = root
        self.units = root / "units"
        self.config = root / "config"
        self.snapshots = root / "snapshots"
        self.scripts = root / "installed-bin"
        self.checkout = root / "checkout"
        self.bin = root / "bin"
        for directory in (self.units, self.config, self.checkout, self.bin):
            directory.mkdir()
        script = (ROOT / "collector/install.sh").read_text()
        for old, new in (
            ("/etc/systemd/system", self.units),
            ("/etc/horatio", self.config),
            ("/usr/local/bin", self.scripts),
            ("/var/lib/horatio", self.snapshots),
        ):
            script = script.replace(old, str(new))
        (self.checkout / "install.sh").write_text(script)
        for name in (
            "health-collect.sh",
            "health-sensors.sh",
            "health-config.example.json",
            "horatio-collect.service",
            "horatio-collect.timer",
            "horatio-sensors.service",
            "horatio-sensors.timer",
        ):
            shutil.copy2(ROOT / "collector" / name, self.checkout / name)
        mock = self.bin / "mock"
        mock.write_text(f"#!{sys.executable}\n{MOCK}")
        mock.chmod(0o755)
        for name in ("id", "systemctl", "journalctl", "smartctl", "install"):
            (self.bin / name).symlink_to(mock)
        for name in ("dirname", "basename", "rm"):
            real = shutil.which(name)
            assert real is not None
            (self.bin / name).symlink_to(real)

    def run(
        self: Installation, *args: str, fail: str = ""
    ) -> subprocess.CompletedProcess[str]:
        install = shutil.which("install")
        bash = shutil.which("bash")
        assert install is not None and bash is not None
        return subprocess.run(
            [bash, str(self.checkout / "install.sh"), *args],
            env=dict(
                os.environ,
                PATH=str(self.bin),
                COLLECTOR_TEST=str(self.root),
                REAL_INSTALL=install,
                SYSTEMCTL_FAIL=fail,
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def events(self: Installation) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in (self.root / "events").read_text().splitlines()
        ]


@pytest.fixture
def installation(tmp_path: Path) -> Installation:
    return Installation(tmp_path)


def test_uninstall_and_reinstall_preserve_config_and_history(
    installation: Installation,
) -> None:
    box = installation
    config = box.config / "config.json"
    config.write_text('{"custom": true}\n')
    logs = box.checkout / "logs"
    logs.mkdir()
    history = logs / "events.jsonl"
    history.write_text('{"event": "raised"}\n')
    memory = logs / "alarms-state.json"
    memory.write_text('{"state": {"alarms": {"old": {}}}}\n')
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    box.snapshots.mkdir()
    (box.snapshots / "state.json").write_text("{}")
    (box.snapshots / "sensors.json").write_text("{}")

    result = box.run("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    marker = box.config / "collectors-disabled"
    assert marker.is_file() and marker.stat().st_mode & 0o777 == 0o644
    assert not list(box.units.iterdir()) and not box.snapshots.exists()
    assert not list(box.scripts.iterdir())
    assert config.read_text() == '{"custom": true}\n'
    assert history.read_text() == '{"event": "raised"}\n'
    assert memory.read_text() == '{"state": {"alarms": {"old": {}}}}\n'

    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists() and len(list(box.units.iterdir())) == 4
    starts = [
        event for event in box.events() if event["argv"][:2] == ["systemctl", "start"]
    ]
    assert len(starts) == 4 and all(not event["disabled"] for event in starts)
    assert config.read_text() == '{"custom": true}\n'
    assert history.read_text() == '{"event": "raised"}\n'


@pytest.mark.parametrize("fail", ["disable", "daemon-reload"])
def test_failed_uninstall_does_not_mark_collectors_removed(
    installation: Installation, fail: str
) -> None:
    assert installation.run().returncode == 0
    assert installation.run("--uninstall", fail=fail).returncode != 0
    assert not (installation.config / "collectors-disabled").exists()


def test_failed_first_run_still_restores_collector_expectation(
    installation: Installation,
) -> None:
    marker = installation.config / "collectors-disabled"
    marker.touch()
    assert installation.run(fail="start").returncode != 0
    assert not marker.exists()
    assert len(list(installation.units.iterdir())) == 4
