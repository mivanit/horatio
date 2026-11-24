"""Real installers on a disposable systemd host; never part of the default suite.

HORATIO_SYSTEMD_TEST=1 uv run --locked pytest tests/deployment/test_systemd.py -k python
HORATIO_SYSTEMD_TEST=1 uv run --locked pytest tests/deployment/test_systemd.py -k lua

Needs passwordless sudo and the documented installation prerequisites. Refuses
hosts with an existing Horatio installation. Services use their shipped sandbox
and timer settings; no commands, metrics, or native modules are mocked.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tests.support import ROOT

BASELINE = "v0.2.1"
PASSWORD = "deployment-test-password"
UNITS = (
    "horatio.service",
    "horatio-collect.service",
    "horatio-collect.timer",
    "horatio-sensors.service",
    "horatio-sensors.timer",
    "horatio-lua.service",
    "horatio-lua-history.service",
    "horatio-lua-history.timer",
)
pytestmark = pytest.mark.skipif(
    os.environ.get("HORATIO_SYSTEMD_TEST") != "1",
    reason="requires an explicitly opted-in disposable systemd host",
)


def run(*args: str, cwd: Path = ROOT, input: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        input=input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
        start_new_session=True,  # password CLI reads the supplied stdin, not a TTY
    )
    assert result.returncode == 0, f"{args}:\n{result.stdout}"
    return result.stdout.strip()


def eventually(check: Callable[[], Any], timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            assert result
            return result
        except (AssertionError, OSError, URLError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def request(path: str, *, password: bool = False) -> bytes:
    headers = {"X-Dashboard-Password": PASSWORD} if password else {}
    opener = build_opener(ProxyHandler({}))
    with opener.open(
        Request("http://127.0.0.1:8080" + path, headers=headers), timeout=15
    ) as response:
        return response.read()


def api(path: str) -> Any:
    return json.loads(request(path))


def records(app: Path) -> list[dict[str, Any]]:
    # Ignore a concurrently appended, incomplete last line.
    data = (app / "logs/history.jsonl").read_bytes()
    return [json.loads(line) for line in data.split(b"\n")[:-1] if line]


def checkout(app: Path, ref: str) -> None:
    if not (app / ".git").exists():
        run("git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(app))
    run("git", "checkout", "--quiet", "--detach", ref, cwd=app)
    if ref == "HEAD":
        # Include tracked working-tree edits in local runs, without copying any
        # personal config, credentials, history, or runtime from the source.
        for name in run("git", "ls-files", "-z").split("\0"):
            if not name:
                continue
            source, target = ROOT / name, app / name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif target.exists():
                target.unlink()


def configure(app: Path) -> None:
    config = json.loads((app / "config.example.json").read_text())
    config.update(title="preserved deployment config", ping_targets=[], http_targets=[])
    config["history"].update(record_interval=10, sample_duration=1, sample_count=1)
    (app / "config.json").write_text(json.dumps(config) + "\n")


@pytest.fixture
def installed_host() -> Iterator[Path]:
    assert os.geteuid() != 0, "run as an ordinary user with passwordless sudo"
    assert Path("/run/systemd/system").is_dir(), "requires booted systemd"
    run("sudo", "-n", "true")
    # A filtered list-unit-files exits 1 when it finds nothing on Ubuntu's
    # systemd. List everything and filter here, keeping command errors fatal.
    for command in ("list-unit-files", "list-units"):
        listing = run("systemctl", command, "--all", "--no-legend", "--no-pager")
        assert not any(
            line.lstrip().startswith("horatio") for line in listing.splitlines()
        ), f"refusing existing Horatio units: {listing}"
    for path in (
        "/etc/horatio",
        "/var/lib/horatio",
        "/usr/local/bin/health-collect.sh",
        "/usr/local/bin/health-sensors.sh",
    ):
        assert not Path(path).exists(), f"refusing existing installation: {path}"
    for port in (8080, 61208):
        with socket.socket() as probe:
            # A stopped server can leave connections in TIME_WAIT. Match the
            # servers' reuse setting while still rejecting an active listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            probe.listen()
    # A /tmp checkout would evade the ProtectHome/managed-interpreter regression.
    with tempfile.TemporaryDirectory(
        prefix="horatio-deployment-", dir=Path.home()
    ) as directory:
        app = Path(directory) / "checkout"
        try:
            yield app
        finally:
            # Also captures failed startup/installer output in pytest's report.
            print(
                run(
                    "sudo",
                    "journalctl",
                    "--no-pager",
                    "-n",
                    "150",
                    *[arg for unit in UNITS for arg in ("-u", unit)],
                )
            )
            for log in (app / "logs").glob("*.log"):
                print(f"{log}:\n{log.read_text(errors='replace')[-12000:]}")
            # Disable timers first so they cannot restart services during cleanup.
            for unit in sorted(UNITS, key=lambda name: not name.endswith(".timer")):
                if Path("/etc/systemd/system", unit).exists():
                    run("sudo", "systemctl", "disable", "--now", unit)
                    run("sudo", "rm", "-f", "/etc/systemd/system/" + unit)
            run("sudo", "systemctl", "daemon-reload")
            run("sudo", "rm", "-rf", "/etc/horatio", "/var/lib/horatio")
            run(
                "sudo",
                "rm",
                "-f",
                "/usr/local/bin/health-collect.sh",
                "/usr/local/bin/health-sensors.sh",
            )


def python_ready(app: Path) -> None:
    assert (app / ".venv/bin/python").resolve().is_relative_to(Path.home()), (
        "use a managed Python under home to exercise the service's interpreter bind"
    )
    eventually(lambda: api("/api/4/mem")["total"] > 0)
    assert api("/api/version")["version"] == (app / "VERSION").read_text().strip()
    assert b"static/app.js" in request("/")
    eventually(lambda: len(records(app)) >= 2)
    assert records(app)[-1]["memory"]["total_gb"] > 0
    assert api("/api/history?hours=1")["count"] >= 2
    assert (
        run("systemctl", "show", "horatio.service", "-p", "NRestarts", "--value") == "0"
    )


def test_python_fresh_install(installed_host: Path) -> None:
    app = installed_host
    checkout(app, "HEAD")
    configure(app)
    run("bash", "install.sh", "--no-optional", cwd=app)
    python_ready(app)
    before = records(app)
    run("sudo", "systemctl", "restart", "horatio.service")
    after_restart = records(app)
    python_ready(app)
    eventually(lambda: len(records(app)) > len(after_restart))
    assert records(app)[: len(before)] == before


def test_python_upgrade_from_published_release(installed_host: Path) -> None:
    app = installed_host
    candidate = run("git", "rev-parse", "HEAD")
    checkout(app, BASELINE)
    configure(app)
    run("bash", "install.sh", "--no-optional", "--collector", cwd=app)
    python_ready(app)
    # Exercise the real password CLI and then use the credential over HTTP.
    run(
        str(app / ".venv/bin/python"),
        "server.py",
        "--set-password",
        cwd=app,
        input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert request("/api/history/raw", password=True)
    collector_config = json.loads(Path("/etc/horatio/config.json").read_text())
    # Keep runner noise from pushing our later fault past the displayed limit.
    collector_config["journal_ignore"] = [
        "deployment-preserved-marker",
        "^(?!.*horatio-deployment-alarm)",
    ]
    run(
        "sudo",
        "tee",
        "/etc/horatio/config.json",
        input=json.dumps(collector_config) + "\n",
    )
    saved = {
        path: path.read_bytes()
        for path in (
            app / "config.json",
            app / ".password",
            Path("/etc/horatio/config.json"),
        )
    }
    before = records(app)
    run("git", "checkout", "--quiet", "--detach", candidate, cwd=app)
    checkout(app, "HEAD")
    # No --collector here: reinstall must detect and refresh the existing setup.
    run("bash", "install.sh", "--no-optional", cwd=app)
    python_ready(app)
    assert "python/server.py" in run(
        "systemctl", "show", "horatio.service", "-p", "ExecStart", "--value"
    )
    for path, content in saved.items():
        assert path.read_bytes() == content, path
    assert records(app)[: len(before)] == before
    assert request("/api/history/raw", password=True)
    for name in ("collect", "sensors"):
        run("systemctl", "is-active", "--quiet", f"horatio-{name}.timer")
        run("systemctl", "is-enabled", "--quiet", f"horatio-{name}.timer")
        script = Path(f"/usr/local/bin/health-{name}.sh")
        assert script.stat().st_uid == 0
        assert script.read_bytes() == (app / "collector" / script.name).read_bytes()
    assert api("/api/storage-health")["status"] == "ok"
    assert api("/api/sensors")["status"] == "ok"
    run("sudo", "systemctl", "restart", "horatio.service")
    after_restart = records(app)
    python_ready(app)
    eventually(lambda: len(records(app)) > len(after_restart))
    assert records(app)[: len(before)] == before
    assert request("/api/history/raw", password=True)

    # A real journal fault gives the tracker something to preserve through
    # removal, rather than letting an empty alarm map satisfy this test.
    message = "horatio-deployment-alarm"
    run("systemd-cat", "--priority=err", input=message + "\n")
    run("sudo", "journalctl", "--sync")
    run("sudo", "systemctl", "start", "horatio-collect.service")
    memory_path = app / "logs/alarms-state.json"

    def remembered_fault() -> dict[str, Any]:
        memory = json.loads(memory_path.read_text())
        return next(
            (
                entry
                for entry in memory.get("state", {}).get("alarms", {}).values()
                if message in entry["text"]
            ),
            {},
        )

    fault = eventually(remembered_fault)
    before = records(app)
    events_path = app / "logs/events.jsonl"
    events_before = events_path.read_bytes()
    pid = run("systemctl", "show", "horatio.service", "-p", "MainPID", "--value")
    run("sudo", "bash", "collector/install.sh", "--uninstall", cwd=app)

    def collectors_removed() -> bool:
        for endpoint in ("/api/storage-health", "/api/sensors"):
            snapshot = api(endpoint)
            assert snapshot["status"] == "missing"
            assert snapshot["expected"] is False
        return True

    eventually(collectors_removed)
    assert (
        run("systemctl", "show", "horatio.service", "-p", "MainPID", "--value") == pid
    )
    assert remembered_fault() == fault
    run("sudo", "systemctl", "restart", "horatio.service")
    after_restart = records(app)
    python_ready(app)
    eventually(collectors_removed)
    eventually(lambda: len(records(app)) > len(after_restart))
    assert remembered_fault() == fault
    assert records(app)[: len(before)] == before
    assert events_path.read_bytes().startswith(events_before)

    # Reinstall the collector while the restarted dashboard stays running.
    pid = run("systemctl", "show", "horatio.service", "-p", "MainPID", "--value")
    run("sudo", "bash", "collector/install.sh", cwd=app)
    assert not Path("/etc/horatio/collectors-disabled").exists()
    for endpoint in ("/api/storage-health", "/api/sensors"):
        eventually(lambda: api(endpoint)["status"] == "ok")
        assert api(endpoint)["expected"] is True
    eventually(
        lambda: any(
            row["source"] == "state"
            and row["key"] == fault["key"]
            and row["since"] == fault["since"]
            for row in api("/api/alarms")["active"]
        )
    )
    assert (
        run("systemctl", "show", "horatio.service", "-p", "MainPID", "--value") == pid
    )
    for path, content in saved.items():
        assert path.read_bytes() == content, path
    assert records(app)[: len(before)] == before
    assert events_path.read_bytes().startswith(events_before)


def test_lua_installed_service_and_unattended_timer(installed_host: Path) -> None:
    app = installed_host
    checkout(app, "HEAD")
    configure(app)
    saved_config = (app / "config.json").read_bytes()
    run("sh", "lua/install-linux.sh", cwd=app)
    assert api("/api/4/mem")["total"] > 0
    assert api("/api/version")["version"] == (app / "VERSION").read_text().strip()
    assert b"static/app.js?v=" in request("/")
    run("systemctl", "is-enabled", "--quiet", "horatio-lua-history.timer")
    run("systemctl", "is-active", "--quiet", "horatio-lua-history.timer")
    # Readiness can finish while the first timer-triggered write is still running.
    before = eventually(lambda: records(app))
    trigger = run(
        "systemctl",
        "show",
        "horatio-lua-history.timer",
        "-p",
        "LastTriggerUSecMonotonic",
        "--value",
    )

    def timer_recorded() -> bool:
        # No HTTP requests here: piggyback recording must not satisfy this check.
        fired = run(
            "systemctl",
            "show",
            "horatio-lua-history.timer",
            "-p",
            "LastTriggerUSecMonotonic",
            "--value",
        )
        return fired != trigger and len(records(app)) > len(before)

    eventually(timer_recorded, timeout=390)  # the shipped five-minute timer
    assert (
        run(
            "systemctl",
            "show",
            "horatio-lua-history.service",
            "-p",
            "Result",
            "--value",
        )
        == "success"
    )
    before = records(app)
    assert before[-1]["memory"]["total_gb"] > 0
    run("sudo", "systemctl", "restart", "horatio-lua.service")
    eventually(lambda: api("/api/history?hours=1")["count"] >= len(before))
    assert records(app)[: len(before)] == before
    assert (app / "www/config.json").read_bytes() == saved_config
    assert (
        run("systemctl", "show", "horatio-lua.service", "-p", "NRestarts", "--value")
        == "0"
    )
