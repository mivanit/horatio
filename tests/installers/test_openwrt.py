"""Run the OpenWrt installer with temporary files and mocked system commands.

Usage: uv run pytest tests/installers/test_openwrt.py
No network, package installation, service changes, or native modules are used.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT

ASSET: bytes = b"// Chart.js test fixture\nwindow.Chart = {};\n"
ASSET_HASH: str = hashlib.sha256(ASSET).hexdigest()
MOCK: str = r"""#!/usr/bin/env python3
import json, os, pathlib, pwd, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["HORATIO_INSTALL_TEST"])
event = {"command": name, "args": args}
if name == "crontab" and args[-1] != "-l":
    event["content"] = pathlib.Path(args[-1]).read_text()
if name == "test-uhttpd-service":
    config = root / "config.json"
    event["config"] = json.loads(config.read_text()) if config.exists() else None
with (root / "events").open("a") as out:
    out.write(json.dumps(event) + "\n")
if [name, *args] == json.loads(os.environ.get("HORATIO_FAIL_COMMAND", "[]")):
    print("injected command failure", file=sys.stderr)
    sys.exit(1)
if name == "uci" and args[:2] == ["get", "network.lan.ipaddr"]:
    print(os.environ.get("HORATIO_DETECTED_LAN_IP", "192.168.1.1"))
elif name == "uci" and args == ["-q", "get", "uhttpd.dashboard"]:
    sys.exit(1 if os.environ.get("HORATIO_SECTION_MISSING") == "1" else 0)
elif name == "ip":
    print("    inet 127.0.0.1/8 scope host lo")
    address = os.environ.get("HORATIO_INTERFACE_IP", "192.168.2.1/24")
    if address:
        print("    inet " + address + " scope global br-lan")
elif name == "wget" and args[-1].startswith("https://"):
    dest = pathlib.Path(args[args.index("-qO") + 1])
    mode = os.environ.get("HORATIO_DOWNLOAD_MODE", "success")
    dest.write_bytes((root / "asset").read_bytes() if mode == "success" else b"partial download")
    sys.exit(4 if mode == "failure" else 0)
elif name == "crontab":
    assert args[:2] == ["-c", str(root / "crontabs")], args
    table = pathlib.Path(args[1]) / pwd.getpwuid(os.geteuid()).pw_name
    if args[2:] == ["-l"]:
        if not table.exists():
            sys.exit(1)
        sys.stdout.write(table.read_text())
    else:
        mode = os.environ.get("HORATIO_CRON_WRITE_MODE", "success")
        if mode == "failure":
            print("injected crontab write failure", file=sys.stderr)
            sys.exit(1)
        if mode != "silent":
            table.write_text(event["content"])
elif name == "test-uhttpd-service" and event["config"] is None and os.environ["HORATIO_UNINSTALL"] != "1":
    sys.exit(42)
"""


class TestOpenWrtInstaller:
    @pytest.fixture(autouse=True)
    def setup(self: TestOpenWrtInstaller, tmp_path: Path) -> None:
        self.root: Path = tmp_path
        (self.root / "openwrt").mkdir()
        (self.root / "static").mkdir()
        self.bin: Path = self.root / "bin"
        self.bin.mkdir()
        (self.root / "crontabs").mkdir()
        self.cron: Path = self.root / "crontabs" / pwd.getpwuid(os.geteuid()).pw_name
        self.asset: Path = self.root / "static/chart.umd.min.js"
        (self.root / "asset").write_bytes(ASSET)
        self.example: dict[str, Any] = {
            "title": "Example",
            "ping_targets": [{"address": "8.8.8.8"}],
        }
        (self.root / "config.example.json").write_text(json.dumps(self.example))
        script: str = (ROOT / "openwrt/install.sh").read_text()
        # Redirect system paths to fixtures. The actual installer control flow,
        # checksum utility, temporary file and mv run.
        script = script.replace("/etc/init.d/uhttpd", "test-uhttpd-service")
        script = script.replace("/etc/init.d/cron", "test-cron-service")
        script = script.replace("/etc/crontabs", str(self.root / "crontabs"))
        script = re.sub(
            r'CHARTJS_SHA256="[a-f0-9]+"', f'CHARTJS_SHA256="{ASSET_HASH}"', script
        )
        self.script: Path = self.root / "openwrt/install.sh"
        self.script.write_text(script)
        mock: Path = self.bin / "mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        name: str
        for name in (
            "uci",
            "uhttpd",
            "apk",
            "wget",
            "crontab",
            "sleep",
            "ip",
            "test-uhttpd-service",
            "test-cron-service",
        ):
            (self.bin / name).symlink_to(mock)

    def run_install(
        self: TestOpenWrtInstaller,
        mode: str = "success",
        *,
        extra_env: dict[str, str] | None = None,
        uninstall: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env: dict[str, str] = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            HORATIO_INSTALL_TEST=str(self.root),
            HORATIO_DOWNLOAD_MODE=mode,
            HORATIO_UNINSTALL="1" if uninstall else "0",
        )
        env.pop("LAN_IP", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["sh", str(self.script), *(["--uninstall"] if uninstall else [])],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def events(self: TestOpenWrtInstaller, command: str) -> list[dict[str, Any]]:
        path: Path = self.root / "events"
        entries: list[dict[str, Any]] = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        return [entry for entry in entries if entry["command"] == command]

    def downloads(self: TestOpenWrtInstaller) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.events("wget")
            if entry["args"][-1].startswith("https://")
        ]

    def cron_writes(self: TestOpenWrtInstaller) -> list[dict[str, Any]]:
        return [entry for entry in self.events("crontab") if "content" in entry]

    def test_download_failure_is_retried_and_config_precedes_restart(
        self: TestOpenWrtInstaller,
    ) -> None:
        failed: subprocess.CompletedProcess[str] = self.run_install("failure")
        assert failed.returncode != 0
        assert not self.asset.exists()
        assert list((self.root / "static").glob(".chart.*")) == []
        assert self.events("test-uhttpd-service") == []
        recovered: subprocess.CompletedProcess[str] = self.run_install()
        assert recovered.returncode == 0, recovered.stderr + recovered.stdout
        assert self.asset.read_bytes() == ASSET
        assert len(self.downloads()) == 2
        assert self.events("test-uhttpd-service")[0]["config"] == self.example
        assert all((entry["args"][1] != str(self.asset) for entry in self.downloads()))

    def test_existing_empty_or_partial_asset_is_repaired(
        self: TestOpenWrtInstaller, subtests: pytest.Subtests
    ) -> None:
        old: bytes
        for old in (b"", b"old partial download"):
            with subtests.test(old=old):
                self.asset.write_bytes(old)
                result: subprocess.CompletedProcess[str] = self.run_install()
                assert result.returncode == 0, result.stderr + result.stdout
                assert self.asset.read_bytes() == ASSET

    def test_checksum_mismatch_keeps_previous_asset_and_does_not_restart(
        self: TestOpenWrtInstaller,
    ) -> None:
        previous: bytes = b"previous asset"
        self.asset.write_bytes(previous)
        result: subprocess.CompletedProcess[str] = self.run_install("mismatch")
        assert result.returncode != 0
        assert self.asset.read_bytes() == previous
        assert list((self.root / "static").glob(".chart.*")) == []
        assert self.events("test-uhttpd-service") == []

    def test_valid_asset_and_existing_config_are_preserved(
        self: TestOpenWrtInstaller,
    ) -> None:
        self.asset.write_bytes(ASSET)
        custom: dict[str, Any] = {"title": "My Router", "ping_targets": []}
        (self.root / "config.json").write_text(json.dumps(custom))
        result: subprocess.CompletedProcess[str] = self.run_install("failure")
        assert result.returncode == 0, result.stderr + result.stdout
        assert self.downloads() == []
        assert self.events("test-uhttpd-service")[0]["config"] == custom

    def test_pins_match_linux_installer(self: TestOpenWrtInstaller) -> None:
        key: str
        for key in ("CHARTJS_VERSION", "CHARTJS_SHA256"):
            pattern: str = rf'^{key}="([^"]+)"$'
            router: re.Match[str] | None = re.search(
                pattern, (ROOT / "openwrt/install.sh").read_text(), re.MULTILINE
            )
            linux: re.Match[str] | None = re.search(
                pattern, (ROOT / "install.sh").read_text(), re.MULTILINE
            )
            assert router is not None
            assert linux is not None
            assert router.group(1) == linux.group(1)

    def test_lan_ip_override_skips_detection_and_strips_cidr(
        self: TestOpenWrtInstaller,
        subtests: pytest.Subtests,
    ) -> None:
        address: str
        for address in ("192.168.50.1", "192.168.50.1/24"):
            with subtests.test(address=address):
                result: subprocess.CompletedProcess[str] = self.run_install(
                    extra_env={"LAN_IP": address}
                )
                assert result.returncode == 0, result.stderr + result.stdout
                assert [
                    "add_list",
                    "uhttpd.dashboard.listen_http=192.168.50.1:8080",
                ] in [event["args"] for event in self.events("uci")]
                assert ["get", "network.lan.ipaddr"] not in [
                    event["args"] for event in self.events("uci")
                ]
                assert self.events("ip") == []
                assert (
                    self.events("wget")[-1]["args"][-1]
                    == "http://192.168.50.1:8080/api/4/load"
                )

    def test_empty_override_uses_detected_lan_ip(self: TestOpenWrtInstaller) -> None:
        result: subprocess.CompletedProcess[str] = self.run_install(
            extra_env={"LAN_IP": "", "HORATIO_DETECTED_LAN_IP": "192.168.3.1/24"}
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert ["add_list", "uhttpd.dashboard.listen_http=192.168.3.1:8080"] in [
            event["args"] for event in self.events("uci")
        ]
        assert self.events("ip") == []

    def test_missing_uci_address_falls_back_to_non_loopback_interface(
        self: TestOpenWrtInstaller,
    ) -> None:
        result: subprocess.CompletedProcess[str] = self.run_install(
            extra_env={"HORATIO_DETECTED_LAN_IP": ""}
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert ["add_list", "uhttpd.dashboard.listen_http=192.168.2.1:8080"] in [
            event["args"] for event in self.events("uci")
        ]
        assert len(self.events("ip")) == 1

    def test_only_owned_cron_entries_are_replaced_or_removed(
        self: TestOpenWrtInstaller,
        subtests: pytest.Subtests,
    ) -> None:
        unrelated: str = (
            "0 1 * * * /usr/local/bin/dashboard-history-backup.sh\n"
            "5 1 * * * echo dashboard-history\n"
            "10 1 * * * /backup # dashboard-history-backup\n"
            "15 1 * * * /ordinary-job\n"
        )
        owned: str = (
            "0 3,15 * * * /old-job # dashboard-history\n"
            "0 3,15 * * * /duplicate-job # dashboard-history \t\n"
        )
        uninstall: bool
        for uninstall in (False, True):
            with subtests.test(uninstall=uninstall):
                self.cron.write_text(unrelated + owned)
                (self.root / "events").unlink(missing_ok=True)
                result: subprocess.CompletedProcess[str] = self.run_install(
                    uninstall=uninstall
                )
                assert result.returncode == 0, result.stderr + result.stdout
                writes: list[dict[str, Any]] = self.cron_writes()
                assert len(writes) == 1
                final: str = self.cron.read_text()
                assert writes[0]["content"] == final
                if uninstall:
                    assert final == unrelated
                else:
                    expected: str = (
                        "0 3,15 * * * wget -qO /dev/null --post-data='' "
                        "http://192.168.1.1:8080/api/history/record # dashboard-history\n"
                    )
                    assert final == unrelated + expected

    def test_missing_and_empty_cron_tables_are_supported(
        self: TestOpenWrtInstaller, subtests: pytest.Subtests
    ) -> None:
        exists: bool
        for exists in (False, True):
            uninstall: bool
            for uninstall in (False, True):
                with subtests.test(exists=exists, uninstall=uninstall):
                    self.cron.unlink(missing_ok=True)
                    if exists:
                        self.cron.write_text("")
                    result: subprocess.CompletedProcess[str] = self.run_install(
                        uninstall=uninstall
                    )
                    assert result.returncode == 0, result.stderr + result.stdout
                    assert self.cron.read_text().count("# dashboard-history") == (
                        0 if uninstall else 1
                    )

    def test_failed_cron_read_never_replaces_existing_jobs(
        self: TestOpenWrtInstaller,
        subtests: pytest.Subtests,
    ) -> None:
        previous: str = "0 1 * * * /backup\n0 3,15 * * * /record # dashboard-history\n"
        uninstall: bool
        for uninstall in (False, True):
            with subtests.test(uninstall=uninstall):
                self.cron.write_text(previous)
                result: subprocess.CompletedProcess[str] = self.run_install(
                    uninstall=uninstall,
                    extra_env={
                        "HORATIO_FAIL_COMMAND": json.dumps(
                            ["crontab", "-c", str(self.cron.parent), "-l"]
                        ),
                    },
                )
                assert result.returncode != 0
                assert self.cron.read_text() == previous
                assert self.cron_writes() == []
                assert "Dashboard removed." not in result.stdout

    def test_broken_cron_symlink_is_not_treated_as_an_empty_table(
        self: TestOpenWrtInstaller,
    ) -> None:
        self.cron.symlink_to(self.root / "missing-table")
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode != 0
        assert self.cron.is_symlink()
        assert self.cron_writes() == []

    def test_failed_or_silently_ignored_cron_write_is_reported(
        self: TestOpenWrtInstaller,
        subtests: pytest.Subtests,
    ) -> None:
        previous: str = "0 1 * * * /backup\n0 3,15 * * * /record # dashboard-history\n"
        mode: str
        for mode in ("failure", "silent"):
            uninstall: bool
            for uninstall in (False, True):
                with subtests.test(mode=mode, uninstall=uninstall):
                    self.cron.write_text(previous)
                    (self.root / "events").unlink(missing_ok=True)
                    result: subprocess.CompletedProcess[str] = self.run_install(
                        uninstall=uninstall, extra_env={"HORATIO_CRON_WRITE_MODE": mode}
                    )
                    assert result.returncode != 0
                    assert self.cron.read_text() == previous
                    assert len(self.cron_writes()) == 1
                    assert "Dashboard removed." not in result.stdout
                    assert "Dashboard is running!" not in result.stdout

    def test_uninstall_stops_on_uci_or_service_failure_before_removing_cron(
        self: TestOpenWrtInstaller,
        subtests: pytest.Subtests,
    ) -> None:
        previous: str = "0 3,15 * * * /record # dashboard-history\n"
        command: list[str]
        for command in (
            ["uci", "show", "uhttpd"],
            ["uci", "delete", "uhttpd.dashboard"],
            ["uci", "commit", "uhttpd"],
            ["test-uhttpd-service", "restart"],
        ):
            with subtests.test(command=command):
                self.cron.write_text(previous)
                result: subprocess.CompletedProcess[str] = self.run_install(
                    uninstall=True,
                    extra_env={"HORATIO_FAIL_COMMAND": json.dumps(command)},
                )
                assert result.returncode != 0
                assert "injected command failure" in result.stderr
                assert "Dashboard removed." not in result.stdout
                assert self.cron.read_text() == previous
                assert self.cron_writes() == []

    def test_uninstall_missing_section_is_repeatable_and_preserves_data(
        self: TestOpenWrtInstaller,
    ) -> None:
        previous: str = "0 1 * * * /backup\n"
        self.cron.write_text(previous)
        config: Path = self.root / "config.json"
        config.write_text('{"title": "My Router"}\n')
        history: Path = self.root / "logs/history.jsonl"
        history.parent.mkdir()
        history.write_text('{"ts": 42}\n')
        for _ in range(2):
            result: subprocess.CompletedProcess[str] = self.run_install(
                uninstall=True, extra_env={"HORATIO_SECTION_MISSING": "1"}
            )
            assert result.returncode == 0, result.stderr + result.stdout
            assert "Dashboard removed." in result.stdout
            assert self.cron.read_text() == previous
            assert config.read_text() == '{"title": "My Router"}\n'
            assert history.read_text() == '{"ts": 42}\n'
        assert ["delete", "uhttpd.dashboard"] not in [
            event["args"] for event in self.events("uci")
        ]

    def test_install_reports_cron_service_failure(self: TestOpenWrtInstaller) -> None:
        result: subprocess.CompletedProcess[str] = self.run_install(
            extra_env={
                "HORATIO_FAIL_COMMAND": json.dumps(["test-cron-service", "restart"]),
            }
        )
        assert result.returncode != 0
        assert "Dashboard is running!" not in result.stdout
