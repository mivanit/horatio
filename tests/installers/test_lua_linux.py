"""Exercise the Linux Lua installer without installing packages or services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.support import ROOT

MOCK = r"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["INSTALL_TEST"])
with (root / "events").open("a") as out:
    out.write(json.dumps([name, *args]) + "\n")
if name == "sudo":
    sys.exit(subprocess.call(args))
if name == "id":
    print({"-u": os.environ.get("TEST_UID", "1000"), "-un": "dashboard", "-gn": "dashboard"}[args[0]])
elif name == "wget" and args[-1].startswith("https:"):
    pathlib.Path(args[args.index("-qO") + 1]).write_bytes(b"chart fixture\n")
elif name == "systemctl" and args[0] == "is-active":
    sys.exit(int(os.environ.get("SERVICE_FAILURE", "0")))
"""


class Installation:
    def __init__(self: Installation, directory: Path) -> None:
        self.root = directory
        for name in ("lua", "scripts", "static", "themes", "bin", "units"):
            (directory / name).mkdir()
        (directory / "index.html").write_text('<script src="/static/app.js"></script>')
        (directory / "static/app.js").write_text("// app")
        (directory / "config.example.json").write_text('{"title": "example"}')
        (directory / "VERSION").write_text("1.0.0\n")
        (directory / "themes/light.css").write_text(":root {}")
        installer = (ROOT / "lua/install-linux.sh").read_text()
        (directory / "lua/install-linux.sh").write_text(
            installer.replace("/etc/systemd/system", str(directory / "units"))
        )
        helper = (ROOT / "lua/prepare-web.sh").read_text()
        digest = hashlib.sha256(b"chart fixture\n").hexdigest()
        (directory / "lua/prepare-web.sh").write_text(
            re.sub(r'CHARTJS_SHA256="[a-f0-9]+"', f'CHARTJS_SHA256="{digest}"', helper)
        )
        (directory / "scripts/setup-lua-runtime.sh").write_text(
            "#!/bin/sh\nset -eu\n"
            'echo build >> "$INSTALL_TEST/builds"\n'
            '[ "${BUILD_FAILURE:-0}" = 0 ] || exit 1\n'
            'mkdir -p "$1/bin"\n'
            'printf "#!/bin/sh\\nexit 0\\n" > "$1/bin/uhttpd"\n'
            'chmod +x "$1/bin/uhttpd"\n'
        )
        mock = directory / "bin/mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("sudo", "id", "wget", "systemctl", "sleep"):
            (directory / "bin" / name).symlink_to(mock)

    def run(
        self: Installation, *args: str, **env: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for key in ("LISTEN_ADDRESS", "PORT"):
            environment.pop(key, None)
        environment.update(
            INSTALL_TEST=str(self.root),
            PATH=f"{self.root / 'bin'}:{environment['PATH']}",
            **env,
        )
        return subprocess.run(
            ["sh", str(self.root / "lua/install-linux.sh"), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def events(self: Installation) -> list[list[str]]:
        path = self.root / "events"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )


@pytest.fixture
def installation(tmp_path: Path) -> Installation:
    return Installation(tmp_path)


def test_install_is_private_and_preserves_config_history(
    installation: Installation,
) -> None:
    root = installation.root
    (root / "config.json").write_text('{"title": "custom"}')
    (root / "logs").mkdir()
    history = root / "logs/history.jsonl"
    history.write_text('{"ts": 1}\n')
    result = installation.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((root / "www/config.json").read_text())["title"] == "custom"
    assert (root / "config.json").is_symlink()
    assert history.read_text() == '{"ts": 1}\n'
    unit = (root / "units/horatio-lua.service").read_text()
    assert "User=dashboard\n" in unit
    assert "-p 127.0.0.1:8080" in unit
    assert f"-h {root}/www " in unit
    assert "-D -S" in unit
    assert "ProtectSystem=strict" in unit
    assert f"BindPaths={root}/logs\n" in unit
    assert not (root / "www/scripts").exists()
    assert (root / "www/themes/index.json").read_text() == '["light"]\n'
    assert (
        "OnUnitActiveSec=5min" in (root / "units/horatio-lua-history.timer").read_text()
    )
    assert installation.run().returncode == 0
    assert (root / "builds").read_text().splitlines() == ["build"]
    recipe = root / "scripts/setup-lua-runtime.sh"
    recipe.write_text(recipe.read_text() + "# revised dependency\n")
    assert installation.run().returncode == 0
    assert (root / "builds").read_text().splitlines() == ["build", "build"]


def test_wildcard_listener_ticks_loopback(installation: Installation) -> None:
    result = installation.run(LISTEN_ADDRESS="0.0.0.0", PORT="8181")
    assert result.returncode == 0, result.stdout + result.stderr
    service = (installation.root / "units/horatio-lua.service").read_text()
    tick = (installation.root / "units/horatio-lua-history.service").read_text()
    assert "-p 0.0.0.0:8181" in service
    assert "http://127.0.0.1:8181/api/history/tick" in tick


@pytest.mark.parametrize(
    "env",
    [
        {"PORT": "80"},
        {"PORT": "8080%"},
        {"LISTEN_ADDRESS": "999.1.1.1"},
        {"TEST_UID": "0"},
        {"BUILD_FAILURE": "1"},
    ],
)
def test_preflight_or_build_failure_installs_no_units(
    installation: Installation, env: dict[str, str]
) -> None:
    assert installation.run(**env).returncode != 0
    assert not list((installation.root / "units").iterdir())
    assert not any(event[0] == "sudo" for event in installation.events())


def test_readiness_failure_is_reported(installation: Installation) -> None:
    result = installation.run(SERVICE_FAILURE="1")
    assert result.returncode != 0
    assert "failed its readiness check" in result.stderr


def test_uninstall_keeps_data_and_runtime(installation: Installation) -> None:
    assert installation.run().returncode == 0
    result = installation.run("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((installation.root / "units").iterdir())
    assert (installation.root / "www/config.json").exists()
    assert (installation.root / ".lua-runtime/bin/uhttpd").exists()
    assert installation.run("--uninstall").returncode == 0


def test_help_and_bad_options_have_no_side_effects(installation: Installation) -> None:
    assert installation.run("--help", TEST_UID="0").returncode == 0
    assert installation.run("--bogus").returncode != 0
    assert installation.events() == []
