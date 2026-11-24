"""`install.sh` (install, --grant-capabilities, --uninstall) with mocked commands.

The installer runs for real against a temporary checkout and a temporary
/etc/systemd/system, on a closed PATH: sudo, systemctl, uv, setcap/getcap,
nethogs and pidstat are fakes that log to `events`. No sudo, network or
services are involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import ROOT

BASH: str = shutil.which("bash") or "/bin/bash"
ASSET: bytes = b"// Chart.js test fixture\n"
MOCK: str = r"""
import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["HORATIO_EVENTS"], "a") as out:
    out.write(json.dumps([name, *args]) + "\n")
if name == "sudo":
    os.execvp(args[0], args)
if name == "uv":  # uv sync --project <checkout> ...: a venv whose home is given
    venv = pathlib.Path(args[args.index("--project") + 1]) / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("#!/bin/sh\n")
    (venv / "bin/python").chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = " + os.environ["HORATIO_PY_HOME"] + "\n")
if name == "getcap" and pathlib.Path(args[0]).name in os.environ.get("HORATIO_CAPS", ""):
    print(args[0] + " cap_sys_ptrace=ep")
"""
FAKES: tuple[str, ...] = ("sudo", "systemctl", "uv", "sleep", "journalctl")
TOOLS: tuple[str, ...] = (
    "id",
    "dirname",
    "sed",
    "cp",
    "mkdir",
    "rm",
    "tee",
    "sha256sum",
)


class Box:
    def __init__(self: Box, tmp_path: Path, checkout: str = "co") -> None:
        self.home: Path = tmp_path / "home/u"
        self.home.mkdir(parents=True)
        self.units: Path = tmp_path / "etc/systemd/system"
        self.units.mkdir(parents=True)
        self.co: Path = tmp_path / checkout
        (self.co / "static").mkdir(parents=True)
        (self.co / "static/chart.umd.min.js").write_bytes(ASSET)
        shutil.copy2(ROOT / "horatio.service", self.co)
        shutil.copy2(ROOT / "config.example.json", self.co)
        script: str = (ROOT / "install.sh").read_text()
        for old, new in (
            # Host /usr/sbin tools (nethogs, getcap, setcap) must not leak in.
            ('PATH="$PATH:/usr/sbin:/sbin"\n', ""),
            ("/etc/systemd/system", str(self.units)),
        ):
            assert old in script, old
            script = script.replace(old, new)
        script = re.sub(
            r'CHARTJS_SHA256="[a-f0-9]+"',
            f'CHARTJS_SHA256="{hashlib.sha256(ASSET).hexdigest()}"',
            script,
        )
        (self.co / "install.sh").write_text(script)
        self.bin: Path = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "mock").write_text(f"#!{sys.executable}\n{MOCK}")
        (self.bin / "mock").chmod(0o755)
        self.fake(*FAKES)
        name: str
        for name in TOOLS:
            real: str | None = shutil.which(name)
            assert real is not None, name
            (self.bin / name).symlink_to(real)
        self.events_file: Path = tmp_path / "events"
        self.unit: Path = self.units / "horatio.service"

    def fake(self: Box, *names: str) -> None:
        name: str
        for name in names:
            (self.bin / name).symlink_to("mock")

    def run(
        self: Box, *args: str, py_home: str = "/usr/bin", caps: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(self.co / "install.sh"), "--no-optional", *args],
            env=dict(
                os.environ,
                PATH=str(self.bin),
                HOME=str(self.home),
                HORATIO_EVENTS=str(self.events_file),
                HORATIO_PY_HOME=py_home,
                HORATIO_CAPS=caps,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def events(self: Box, command: str | None = None) -> list[list[str]]:
        lines: list[str] = (
            self.events_file.read_text().splitlines()
            if self.events_file.exists()
            else []
        )
        events: list[list[str]] = [json.loads(line) for line in lines]
        return [e for e in events if command is None or e[0] == command]

    def directive(self: Box, key: str) -> str:
        found: re.Match[str] | None = re.search(
            rf"^{key}=(.*)$", self.unit.read_text(), re.MULTILINE
        )
        assert found is not None, key
        return found.group(1)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_uv_python_is_bound_through_its_minor_version_link(box: Box) -> None:
    uv_python: Path = box.home / ".local/share/uv/python"
    (uv_python / "cpython-3.13.5-linux-x86_64-gnu/bin").mkdir(parents=True)
    (uv_python / "cpython-3.13-linux-x86_64-gnu").symlink_to(
        "cpython-3.13.5-linux-x86_64-gnu"
    )
    link: Path = uv_python / "cpython-3.13-linux-x86_64-gnu"
    result: subprocess.CompletedProcess[str] = box.run(py_home=f"{link}/bin")
    assert result.returncode == 0, result.stderr + result.stdout
    assert box.directive("BindReadOnlyPaths") == f"{box.co} {link}"
    assert box.directive("ExecStart") == f"{box.co}/.venv/bin/python python/server.py"
    assert box.directive("User") == pwd.getpwuid(os.geteuid()).pw_name
    assert "/opt/horatio" not in box.unit.read_text()
    assert (box.co / "logs").is_dir() and (box.co / "config.json").exists()
    assert box.events("systemctl")[-3:] == [
        ["systemctl", "enable", "horatio.service"],
        ["systemctl", "restart", "horatio.service"],
        ["systemctl", "is-active", "--quiet", "horatio.service"],
    ]


def test_system_python_binds_usr_and_unit_verifies(box: Box) -> None:
    result: subprocess.CompletedProcess[str] = box.run(py_home="/usr/bin")
    assert result.returncode == 0, result.stderr + result.stdout
    assert box.directive("BindReadOnlyPaths") == f"{box.co} /usr"
    analyze: str | None = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed")
    # verify exits 0 on parse warnings ("Unknown key", bad paths): stderr is the signal.
    verify: subprocess.CompletedProcess[str] = subprocess.run(
        [analyze, "verify", "--man=no", str(box.unit)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert verify.returncode == 0 and verify.stderr == "", verify.stderr


def test_existing_config_is_never_overwritten(box: Box) -> None:
    (box.co / "config.json").write_text('{"title": "mine"}\n')
    result: subprocess.CompletedProcess[str] = box.run()
    assert result.returncode == 0, result.stderr + result.stdout
    assert (box.co / "config.json").read_text() == '{"title": "mine"}\n'


@pytest.mark.parametrize("prefix", ["home", "parent", "root"])
def test_prefix_containing_home_is_refused(box: Box, prefix: str) -> None:
    py_home: str = {
        "home": f"{box.home}/bin",
        "parent": f"{box.home.parent}/bin",
        "root": "/bin",
    }[prefix]
    result: subprocess.CompletedProcess[str] = box.run(py_home=py_home)
    assert result.returncode == 1 and "contains your home" in result.stderr
    assert box.events("sudo") == [] and not box.unit.exists()


def test_help_prints_the_header_and_changes_nothing(box: Box) -> None:
    for flag in ("--help", "-h"):
        result: subprocess.CompletedProcess[str] = box.run("--collector", flag)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("Horatio installer and updater.")
        assert "./install.sh --collector" in result.stdout
        assert "-h | --help" in result.stdout and "set -euo" not in result.stdout
    assert box.events() == [] and not box.unit.exists()
    unknown: subprocess.CompletedProcess[str] = box.run("--nope")
    assert unknown.returncode == 2 and "./install.sh --help" in unknown.stderr


def test_bad_characters_refused_before_sudo(tmp_path: Path) -> None:
    bad: Box = Box(tmp_path / "a", checkout="co:x")
    result: subprocess.CompletedProcess[str] = bad.run()
    assert result.returncode == 1 and "Unsupported checkout path" in result.stderr
    assert bad.events() == []
    box: Box = Box(tmp_path / "b")
    result = box.run(py_home="/opt/py thon/bin")
    assert result.returncode == 1 and "Unsupported Python path" in result.stderr
    assert box.events("sudo") == [] and not box.unit.exists()


def test_granted_capabilities_fit_the_bounding_set(box: Box) -> None:
    box.fake("setcap", "getcap", "nethogs", "pidstat")
    result: subprocess.CompletedProcess[str] = box.run("--grant-capabilities")
    assert result.returncode == 0, result.stderr + result.stdout
    granted: list[list[str]] = [e[1:] for e in box.events("sudo") if e[1] == "setcap"]
    assert [(e[-1], e[1].endswith("+ep")) for e in granted] == [
        (str(box.bin / "nethogs"), True),
        (str(box.bin / "pidstat"), True),
    ]
    bounding: set[str] = set(box.directive("CapabilityBoundingSet").lower().split())
    e: list[str]
    for e in granted:
        assert set(e[1].removesuffix("+ep").split(",")) <= bounding, e


def test_uninstall_removes_unit_and_only_present_capabilities(box: Box) -> None:
    box.fake("setcap", "getcap", "nethogs", "pidstat")
    box.unit.write_text("[Service]\n")
    result: subprocess.CompletedProcess[str] = box.run("--uninstall", caps="nethogs")
    assert result.returncode == 0, result.stderr + result.stdout
    assert not box.unit.exists()
    sudo: list[list[str]] = [e[1:] for e in box.events("sudo")]
    assert ["systemctl", "disable", "--now", "horatio.service"] in sudo
    assert [e for e in sudo if e[0] == "setcap"] == [
        ["setcap", "-r", str(box.bin / "nethogs")]
    ]
    assert box.events("uv") == []
