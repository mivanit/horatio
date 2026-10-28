"""Exercise nginx installation and recovery using isolated files and services.

Run with uv run pytest tests/installers/test_nginx.py; no privileges, network or nginx required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, TextIO

import pytest

from tests.support import ROOT

MOCK: str = r"""#!/usr/bin/python3
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["HORATIO_NGINX_TEST_ROOT"])
executable = pathlib.Path(sys.argv[0])
name = executable.name + ("-service" if executable.parent.name == "init.d" else "")
args = sys.argv[1:]
log = root / "events"
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
event = {"command": name, "args": args}
if name == "uhttpd-service":
    event["config"] = json.loads((root / "uci.json").read_text())
if name == "wget":
    event["recovery_exists"] = bool(list((root / "etc/nginx").glob(".horatio-*")))
with log.open("a") as f:
    f.write(json.dumps(event) + "\n")
failure = os.environ.get("HORATIO_NGINX_FAIL", "")
if name in ("apk", "opkg"):
    if os.environ.get("HORATIO_PACKAGE_INSTALL", "success") == "success":
        target = root / "bin/nginx"
        if not target.exists(): target.symlink_to(root / "mock")
elif name == "nginx":
    if "-c" in args and not pathlib.Path(args[args.index("-c") + 1]).is_file():
        print("nginx: configuration file missing", file=sys.stderr)
        sys.exit(1)
    if failure == "validate":
        print("nginx: invalid port in upstream", file=sys.stderr)
        sys.exit(1)
elif name == "nginx-util":
    if failure == "generate":
        sys.exit(1)
    if os.environ.get("HORATIO_UCI_ENABLED", "true") == "true":
        (root / "var/lib/nginx/uci.conf").write_text("# generated UCI config\n")
elif name == "systemctl":
    if args[0] == "is-active":
        sys.exit(0 if os.environ.get("HORATIO_WAS_RUNNING", "1") == "1" else 3)
    if args[0] == "reload-or-restart" and failure == "reload":
        if not any(e["command"] == name and e["args"] == args for e in previous):
            sys.exit(1)
elif name == "uci":
    config = root / "uci.json"
    if args == ["-q", "get", "nginx.global.uci_enable"]:
        print(os.environ.get("HORATIO_UCI_ENABLED", "true"))
    elif args[:3] == ["-q", "show", "nginx"]:
        for key, value in json.loads((root / "nginx-uci.json").read_text()).items():
            print(key + "=" + repr(value))
    elif args[:2] == ["-q", "get"]:
        source = root / "nginx-uci.json" if args[2].startswith("nginx.") else config
        data = json.loads(source.read_text())
        if args[2] not in data: sys.exit(1)
        print(data[args[2]])
    elif args[0] == "export":
        sys.stdout.write(config.read_text())
    elif args[0] == "import":
        config.write_text(sys.stdin.read())
    elif args[0] == "set":
        key, value = args[1].split("=", 1)
        data = json.loads(config.read_text()); data[key] = value
        config.write_text(json.dumps(data))
    elif args[:2] == ["-q", "delete"]:
        data = json.loads(config.read_text()); data.pop(args[2], None)
        config.write_text(json.dumps(data))
elif name in ("nginx-service", "uhttpd-service"):
    state_file = root / (name + "-state.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "phase": "initial", "running": name == "uhttpd-service" or os.environ.get("HORATIO_WAS_RUNNING", "1") == "1",
        "checks": 0,
    }
    if args == ["enabled"]:
        enabled = root / "nginx-enabled"
        value = enabled.read_text() if enabled.exists() else os.environ.get("HORATIO_WAS_ENABLED", "1")
        sys.exit(0 if value == "1" else 1)
    if args in (["enable"], ["disable"]):
        (root / "nginx-enabled").write_text("1" if args == ["enable"] else "0")
    if args == ["running"]:
        state["checks"] += 1
        state_file.write_text(json.dumps(state))
        running = state["running"]
        if state["phase"] == "stopping":
            running = state["checks"] <= int(os.environ.get("HORATIO_STOP_DELAY", "0"))
        if (state["phase"] == "candidate" and failure == name.replace("-service", "-async")) or (
            state["phase"] == "rollback" and os.environ.get("HORATIO_FAIL_RESTORE") == name.removesuffix("-service")
        ):
            running = state["checks"] <= 2  # process exists while bind retries run, then exits
        sys.exit(0 if running else 1)
    if args == ["stop"]:
        state = {"phase": "stopping", "running": False, "checks": 0}
    elif args in (["start"], ["restart"]):
        if name == "uhttpd-service":
            candidate = json.loads((root / "uci.json").read_text())["uhttpd.main.listen_http"] == "127.0.0.1:8081"
        else:
            candidate = "proxy_pass" in (root / "etc/nginx/conf.d/example-internal.conf").read_text()
        if os.environ.get("HORATIO_UNINSTALL"):
            candidate = not candidate if name == "uhttpd-service" else False
        state = {"phase": "candidate" if candidate else "rollback", "running": True, "checks": 0}
        if candidate and failure == name:
            state["running"] = False
            state_file.write_text(json.dumps(state))
            sys.exit(1)
    state_file.write_text(json.dumps(state))
elif name == "wget":
    if "--no-check-certificate" in args:
        assert args[:-1] == ["-qO-", "-T", "2", "-Y", "off", "--no-check-certificate"]
        if failure == "luci-probe": sys.exit(4)
        assert (root / "etc/nginx/conf.d/example-internal.conf").exists(), "configuration removed before recovery"
        assert (root / "www/example-internal/index.html").exists(), "index removed before recovery"
        assert (root / "nginx-enabled").read_text() == "0", "nginx may reclaim ports on boot"
        assert json.loads((root / "nginx-service-state.json").read_text())["phase"] == "stopping"
        home = json.loads((root / "uci.json").read_text())["uhttpd.main.home"]
        sys.stdout.write((pathlib.Path(home) / args[-1].rsplit("/", 1)[1]).read_text())
        sys.exit(0)
    assert args == ["-qO-", "-T", "2", "-Y", "off", "--header=Host: example.internal", "http://127.0.0.1/"]
    attempts = sum(e["command"] == "wget" for e in previous)
    if failure == "nginx-async" or attempts < int(os.environ.get("HORATIO_READY_DELAY", "0")):
        sys.exit(4)
    if failure == "wrong-index":
        print("another web service")
    else:
        sys.stdout.write((root / "www/example-internal/index.html").read_text())
elif name == "mv":
    if failure == "restore" and pathlib.Path(args[-2]).name == "config.restore":
        print("injected restore failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(subprocess.run([os.environ["HORATIO_REAL_MV"], *args]).returncode)
elif name == "rm":
    if failure == "remove-index" and args[-1] in (
        str(root / "www/example-internal/index.html"), str(root / "var/www/example-internal/index.html"),
    ):
        print("injected index removal failure", file=sys.stderr)
        sys.exit(1)
    if failure == "remove-config" and args[-1] == str(root / "etc/nginx/sites-enabled/example-internal"):
        print("injected config removal failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(subprocess.run([os.environ["HORATIO_REAL_RM"], *args]).returncode)
"""


class TestNginxInstaller:
    @pytest.fixture(autouse=True)
    def setup(self: TestNginxInstaller, tmp_path: Path) -> None:
        self.prepare_root(tmp_path)

    def prepare_root(self: TestNginxInstaller, root: Path) -> None:
        self.root: Path = root
        root.mkdir(parents=True, exist_ok=True)
        self.bin: Path = self.root / "bin"
        self.bin.mkdir()
        self.nginx: Path = self.root / "etc/nginx"
        directory: str
        for directory in ("sites-enabled", "sites-available", "conf.d"):
            (self.nginx / directory).mkdir(parents=True)
        (self.root / "init.d").mkdir()
        self.mock: Path = self.root / "mock"
        self.mock.write_text(MOCK)
        self.mock.chmod(0o755)
        # A closed PATH allows a real missing-nginx/apk-only OpenWrt scenario,
        # regardless of commands installed on the test host.
        name: str
        for name in (
            "awk",
            "basename",
            "chmod",
            "cp",
            "dirname",
            "grep",
            "head",
            "mkdir",
            "mktemp",
            "mv",
            "rm",
            "rmdir",
            "sed",
            "timeout",
            "tr",
        ):
            command: str | None = shutil.which(name)
            assert command is not None, name
            (self.bin / name).symlink_to(command)
        real_mv: str | None = shutil.which("mv")
        real_rm: str | None = shutil.which("rm")
        assert real_mv is not None and real_rm is not None
        self.env: dict[str, str] = dict(
            os.environ,
            PATH=str(self.bin),
            HORATIO_NGINX_TEST_ROOT=str(self.root),
            HORATIO_REAL_MV=real_mv,
            HORATIO_REAL_RM=real_rm,
        )
        script: str = (ROOT / "nginx/install.sh").read_text()
        script = script.replace("/etc/nginx", str(self.nginx))
        script = script.replace("/etc/init.d", str(self.root / "init.d"))
        script = script.replace("/var/www", str(self.root / "var/www"))
        script = script.replace("/var/log/nginx", str(self.root / "var/log/nginx"))
        script = script.replace("/var/lib/nginx", str(self.root / "var/lib/nginx"))
        script = script.replace('INDEX_DIR="/www/', f'INDEX_DIR="{self.root}/www/')
        self.script: Path = self.root / "install.sh"
        self.script.write_text(script)
        self.services: Path = self.root / "host.services"
        self.services.write_text("# domain: example.internal\ndashboard 8080\n")
        self.original_uci: str = json.dumps(
            {
                "uhttpd.main.listen_http": ["0.0.0.0:80", "[::]:80"],
                "uhttpd.main.listen_https": ["0.0.0.0:443", "[::]:443"],
                "uhttpd.custom.option": "keep this",
                "uhttpd.main.home": str(self.root / "www/luci"),
            },
            indent=2,
        )
        (self.root / "uci.json").write_text(self.original_uci)

    def prepare_uninstall(self: TestNginxInstaller) -> None:
        self.prepare("openwrt")
        self.conf.write_text("# generated configuration\n# proxy_pass\n")
        self.runtime_conf.parent.mkdir(parents=True)
        self.runtime_conf.write_text("# generated UCI configuration\n")
        (self.root / "www/luci").mkdir(parents=True)
        (self.nginx / "module.d").mkdir()
        (self.nginx / "restrict_locally").write_text(
            "allow 127.0.0.0/8;\nallow 192.168.0.0/16;\ndeny all;\n"
        )
        (self.nginx / "uci.conf.template").write_text("""# UCI_CONF_VERSION=1.2
worker_processes auto;
user root;
include module.d/*.module;
events {}
http {
    access_log off;
    log_format openwrt
        '$request_method $scheme://$host$request_uri => $status'
        ' (${body_bytes_sent}B in ${request_time}s) <- $http_referer';
    include mime.types;
    default_type application/octet-stream;
    sendfile on;
    client_max_body_size 128M;
    large_client_header_buffers 2 1k;
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    root /www;
    #UCI_HTTP_CONFIG
    include conf.d/*.conf;
}
""")
        (self.root / "nginx-uci.json").write_text(
            json.dumps(
                {
                    "nginx.global": "main",
                    "nginx.global.uci_enable": "true",
                    "nginx._lan": "server",
                    "nginx._lan.listen": "443 ssl default_server [::]:443 ssl default_server",
                    "nginx._lan.server_name": "_lan",
                    "nginx._lan.include": "restrict_locally conf.d/*.locations",
                    "nginx._lan.uci_manage_ssl": "self-signed",
                    "nginx._lan.ssl_certificate": "/etc/nginx/conf.d/_lan.crt",
                    "nginx._lan.ssl_certificate_key": "/etc/nginx/conf.d/_lan.key",
                    "nginx._lan.ssl_session_cache": "shared:SSL:32k",
                    "nginx._lan.ssl_session_timeout": "64m",
                    "nginx._lan.access_log": "off; # logd openwrt",
                    "nginx._redirect2ssl": "server",
                    "nginx._redirect2ssl.listen": "80 [::]:80",
                    "nginx._redirect2ssl.server_name": "_redirect2ssl",
                    "nginx._redirect2ssl.return": "302 https://$host$request_uri",
                }
            )
        )
        current: dict[str, str] = json.loads(self.original_uci)
        current["uhttpd.main.listen_http"] = "127.0.0.1:8081"
        current.pop("uhttpd.main.listen_https")
        self.original_uci = json.dumps(current, indent=2)
        (self.root / "uci.json").write_text(self.original_uci)

    def run_uninstall(
        self: TestNginxInstaller, failure: str = "", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script), str(self.services), "--uninstall"],
            env=dict(
                self.env, HORATIO_NGINX_FAIL=failure, HORATIO_UNINSTALL="1", **env
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_uninstall_releases_ports_disables_nginx_and_preserves_extra_content(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        asset: Path = self.index.parent / "custom-logo.svg"
        asset.write_text("keep this user asset")
        result: subprocess.CompletedProcess[str] = self.run_uninstall()
        assert result.returncode == 0, result.stderr
        assert not self.conf.exists()
        assert not self.index.exists()
        assert asset.read_text() == "keep this user asset"
        assert (self.root / "nginx-enabled").read_text() == "0"
        events: list[dict[str, Any]] = self.events()
        stop: int = next(
            i
            for i, e in enumerate(events)
            if e["command"] == "nginx-service" and e["args"] == ["stop"]
        )
        restart: int = next(
            i
            for i, e in enumerate(events)
            if e["command"] == "uhttpd-service" and e["args"] == ["restart"]
        )
        assert stop < restart
        assert not any(
            (
                e["command"] == "nginx-service"
                and e["args"] in (["start"], ["restart"])
                for e in events
            )
        )
        assert (
            json.loads((self.root / "uci.json").read_text())["uhttpd.main.listen_http"]
            == "0.0.0.0:80"
        )
        assert list((self.root / "www/luci").iterdir()) == []
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_uninstall_failed_port_handoff_restores_files_uci_and_nginx(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        config: bytes
        index: bytes
        config, index = self.conf.read_bytes(), self.index.read_bytes()
        result: subprocess.CompletedProcess[str] = self.run_uninstall("luci-probe")
        assert result.returncode != 0
        assert "did not serve its verification file" in result.stderr
        assert self.conf.read_bytes() == config
        assert self.index.read_bytes() == index
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert (self.root / "nginx-enabled").read_text() == "1"
        state: dict[str, bool] = json.loads(
            (self.root / "nginx-service-state.json").read_text()
        )
        assert state["running"]
        assert list((self.root / "www/luci").iterdir()) == []
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_uninstall_failed_deferred_removal_restores_original_files(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        config: bytes
        index: bytes
        config, index = self.conf.read_bytes(), self.index.read_bytes()
        (self.bin / "rm").unlink()
        (self.bin / "rm").symlink_to(self.mock)
        result: subprocess.CompletedProcess[str] = self.run_uninstall("remove-index")
        assert result.returncode != 0
        assert "injected index removal failure" in result.stderr
        assert self.conf.read_bytes() == config
        assert self.index.read_bytes() == index
        assert self.conf.stat().st_mode & 511 == 416
        assert self.index.stat().st_mode & 511 == 384
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert (self.root / "nginx-enabled").read_text() == "1"
        assert json.loads((self.root / "nginx-service-state.json").read_text())[
            "running"
        ]
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_uninstall_failed_handoff_preserves_disabled_stopped_state(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        result: subprocess.CompletedProcess[str] = self.run_uninstall(
            "luci-probe", HORATIO_WAS_RUNNING="0", HORATIO_WAS_ENABLED="0"
        )
        assert result.returncode != 0
        assert (self.root / "nginx-enabled").read_text() == "0"
        assert not any(
            (
                e["command"] == "nginx-service" and e["args"] == ["start"]
                for e in self.events()
            )
        )
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert self.conf.is_file() and self.index.is_file()

    def test_uninstall_failed_recovery_retains_backups(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        result: subprocess.CompletedProcess[str] = self.run_uninstall(
            "luci-probe", HORATIO_FAIL_RESTORE="uhttpd"
        )
        assert result.returncode != 0
        assert "recovery was incomplete" in result.stderr
        backups: list[Path] = list(self.nginx.glob(".horatio-*"))
        assert len(backups) == 1
        assert (backups[0] / "config").read_bytes() == self.conf.read_bytes()
        assert (backups[0] / "index").read_bytes() == self.index.read_bytes()
        assert (backups[0] / "uhttpd").read_text() == self.original_uci
        assert list((self.root / "www/luci").iterdir()) == []

    def test_uninstall_unknown_setups_refused_before_any_mutation(
        self: TestNginxInstaller,
        subtests: pytest.Subtests,
        tmp_path: Path,
    ) -> None:
        kind: str
        for kind in (
            "manual",
            "server",
            "root",
            "listen",
            "locations",
            "conf",
            "template",
            "module",
            "restrict",
        ):
            with subtests.test(kind=kind):
                case: TestNginxInstaller = TestNginxInstaller()
                case.prepare_root(tmp_path / kind)
                case.prepare_uninstall()
                data: dict[str, str] = json.loads(
                    (case.root / "nginx-uci.json").read_text()
                )
                env: dict[str, str] = {}
                if kind == "manual":
                    env["HORATIO_UCI_ENABLED"] = "false"
                elif kind == "server":
                    data["nginx.custom"] = "server"
                elif kind == "root":
                    data["nginx._lan.root"] = "/www/custom"
                elif kind == "listen":
                    data["nginx._lan.listen"] = "8443 ssl"
                elif kind in ("locations", "conf"):
                    (case.nginx / "conf.d" / ("custom." + kind)).write_text(
                        "# keep custom site\n"
                    )
                elif kind == "template":
                    f: TextIO
                    with (case.nginx / "uci.conf.template").open("a") as f:
                        f.write("# custom include\ninclude custom.conf;\n")
                elif kind == "module":
                    (case.nginx / "module.d/custom.module").write_text(
                        "include custom.conf;\n"
                    )
                elif kind == "restrict":
                    (case.nginx / "restrict_locally").write_text(
                        "location /custom { proxy_pass http://localhost:9000; }\n"
                    )
                (case.root / "nginx-uci.json").write_text(json.dumps(data))
                config: bytes
                index: bytes
                config, index = case.conf.read_bytes(), case.index.read_bytes()
                result: subprocess.CompletedProcess[str] = case.run_uninstall(**env)
                assert result.returncode != 0
                assert "no files or services were changed" in result.stderr
                assert case.conf.read_bytes() == config
                assert case.index.read_bytes() == index
                assert (case.root / "uci.json").read_text() == case.original_uci
                assert not any(
                    (e["command"].endswith("-service") for e in case.events())
                )
                assert list(case.nginx.glob(".horatio-*")) == []

    def test_uninstall_accepts_stock_ipv4_only_routing(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        path: Path = self.root / "nginx-uci.json"
        data: dict[str, str] = json.loads(path.read_text())
        data["nginx._lan.listen"] = "443 ssl default_server"
        data["nginx._redirect2ssl.listen"] = "80"
        path.write_text(json.dumps(data))
        result: subprocess.CompletedProcess[str] = self.run_uninstall()
        assert result.returncode == 0, result.stderr
        assert not self.index.parent.exists()

    def test_uninstall_invalid_probe_home_does_not_stop_services(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare_uninstall()
        (self.root / "www/luci").rmdir()
        result: subprocess.CompletedProcess[str] = self.run_uninstall()
        assert result.returncode != 0
        assert "not a directory" in result.stderr
        assert self.conf.is_file() and self.index.is_file()
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert not any(
            (
                e["command"].endswith("-service")
                and e["args"] not in (["running"], ["enabled"])
                for e in self.events()
            )
        )

    def prepare(
        self: TestNginxInstaller,
        platform: Literal["ubuntu", "openwrt"] = "ubuntu",
        *,
        nginx: bool = True,
    ) -> None:
        if platform == "ubuntu":
            (self.bin / "systemctl").symlink_to(self.mock)
            self.conf: Path = self.nginx / "sites-enabled/example-internal"
            self.index: Path = self.root / "var/www/example-internal/index.html"
        else:
            command: str
            for command in ("uci", "apk", "nginx-util", "wget", "sleep"):
                (self.bin / command).symlink_to(self.mock)
            service: str
            for service in ("nginx", "uhttpd"):
                (self.root / "init.d" / service).symlink_to(self.mock)
            self.runtime_conf: Path = self.root / "var/lib/nginx/uci.conf"
            (self.nginx / "uci.conf").symlink_to(self.runtime_conf)
            (self.nginx / "nginx.conf").write_text("# manual main config\n")
            self.conf = self.nginx / "conf.d/example-internal.conf"
            self.index = self.root / "www/example-internal/index.html"
        if nginx:
            (self.bin / "nginx").symlink_to(self.mock)
        self.conf.write_text("# previous working config\n")
        self.conf.chmod(0o640)
        self.index.parent.mkdir(parents=True)
        self.index.write_text("previous index\n")
        self.index.chmod(0o600)
        self.default_target: Path = self.nginx / "sites-available/default"
        self.default_target.write_text("# distribution default\n")
        self.default: Path = self.nginx / "sites-enabled/default"
        self.default.symlink_to("../sites-available/default")

    def run_install(
        self: TestNginxInstaller, failure: str = "", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script), str(self.services)],
            env=dict(self.env, HORATIO_NGINX_FAIL=failure, **env),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def events(self: TestNginxInstaller) -> list[dict[str, Any]]:
        path: Path = self.root / "events"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def assert_restored(self: TestNginxInstaller) -> None:
        assert self.conf.read_text() == "# previous working config\n"
        assert self.conf.stat().st_mode & 511 == 416
        assert self.index.read_text() == "previous index\n"
        assert self.index.stat().st_mode & 511 == 384
        assert os.readlink(self.default) == "../sites-available/default"
        assert self.default_target.read_text() == "# distribution default\n"
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_ubuntu_uninstall_preserves_extra_content_and_symlink_targets(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        config_target: Path
        index_target: Path
        config_target, index_target = (
            self.root / "config-source",
            self.root / "index-source",
        )
        self.conf.rename(config_target)
        self.index.rename(index_target)
        self.conf.symlink_to(config_target)
        self.index.symlink_to(index_target)
        asset: Path = self.index.parent / "custom-logo.svg"
        asset.write_text("keep this user asset")
        result: subprocess.CompletedProcess[str] = self.run_uninstall()
        assert result.returncode == 0, result.stderr
        assert not self.conf.is_symlink()
        assert not self.index.is_symlink()
        assert config_target.read_text() == "# previous working config\n"
        assert index_target.read_text() == "previous index\n"
        assert asset.read_text() == "keep this user asset"
        assert self.default.is_symlink()
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_ubuntu_uninstall_validation_failure_restores_files_before_reload(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        result: subprocess.CompletedProcess[str] = self.run_uninstall("validate")
        assert result.returncode != 0
        assert "Done." not in result.stdout
        assert "invalid port" in result.stderr
        self.assert_restored()
        assert not any(
            (e["args"] == ["reload-or-restart", "nginx"] for e in self.events())
        )

    def test_ubuntu_uninstall_reload_failure_restores_symlinks_and_reloads_original(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        config_target: Path
        index_target: Path
        config_target, index_target = (
            self.root / "config-source",
            self.root / "index-source",
        )
        self.conf.rename(config_target)
        self.index.rename(index_target)
        self.conf.symlink_to(config_target)
        self.index.symlink_to(index_target)
        result: subprocess.CompletedProcess[str] = self.run_uninstall("reload")
        assert result.returncode != 0
        assert "Done." not in result.stdout
        self.assert_restored()
        assert self.conf.is_symlink()
        assert self.index.is_symlink()
        assert config_target.read_text() == "# previous working config\n"
        assert index_target.read_text() == "previous index\n"
        assert (
            sum((e["args"] == ["reload-or-restart", "nginx"] for e in self.events()))
            == 2
        )

    def test_ubuntu_uninstall_failure_restores_originally_stopped_service(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        result: subprocess.CompletedProcess[str] = self.run_uninstall(
            "reload", HORATIO_WAS_RUNNING="0"
        )
        assert result.returncode != 0
        self.assert_restored()
        calls: list[list[str]] = [
            e["args"] for e in self.events() if e["command"] == "systemctl"
        ]
        assert calls == [
            ["is-active", "--quiet", "nginx"],
            ["reload-or-restart", "nginx"],
            ["stop", "nginx"],
        ]

    def test_ubuntu_uninstall_removal_failures_restore_files_and_modes(
        self: TestNginxInstaller,
        subtests: pytest.Subtests,
        tmp_path: Path,
    ) -> None:
        failure: str
        for failure in ("remove-config", "remove-index"):
            with subtests.test(failure=failure):
                case: TestNginxInstaller = TestNginxInstaller()
                case.prepare_root(tmp_path / failure)
                case.prepare()
                (case.bin / "rm").unlink()
                (case.bin / "rm").symlink_to(case.mock)
                result: subprocess.CompletedProcess[str] = case.run_uninstall(failure)
                assert result.returncode != 0
                assert "Done." not in result.stdout
                assert "removal failure" in result.stderr
                case.assert_restored()
                reloads: int = sum(
                    e["args"] == ["reload-or-restart", "nginx"] for e in case.events()
                )
                assert reloads == (2 if failure == "remove-index" else 0)

    def test_ubuntu_uninstall_incomplete_recovery_retains_backups(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        self.script.write_text(self.script.read_text().replace("nginx -t\n", "false\n"))
        (self.bin / "mv").unlink()
        (self.bin / "mv").symlink_to(self.mock)
        result: subprocess.CompletedProcess[str] = self.run_uninstall("restore")
        assert result.returncode != 0
        assert "recovery was incomplete" in result.stderr
        backups: list[Path] = list(self.nginx.glob(".horatio-*"))
        assert len(backups) == 1
        assert str(backups[0]) in result.stderr
        assert (backups[0] / "config").read_text() == "# previous working config\n"
        assert (backups[0] / "index").read_text() == "previous index\n"
        assert not self.conf.exists()
        assert self.index.read_text() == "previous index\n"

    def test_ubuntu_uninstall_requires_nginx_before_removing_files(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare(nginx=False)
        result: subprocess.CompletedProcess[str] = self.run_uninstall()
        assert result.returncode != 0
        assert "nginx is unavailable" in result.stderr
        self.assert_restored()
        assert self.events() == []

    def test_rejected_config_restores_files_and_default_symlink(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        result: subprocess.CompletedProcess[str] = self.run_install("validate")
        assert result.returncode != 0
        self.assert_restored()
        assert not any((e["args"][0] == "reload-or-restart" for e in self.events()))

    def test_failed_reload_restores_symlinks_without_changing_their_targets(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        config_target: Path
        index_target: Path
        config_target, index_target = (
            self.root / "config-source",
            self.root / "index-source",
        )
        self.conf.rename(config_target)
        self.index.rename(index_target)
        self.conf.symlink_to(config_target)
        self.index.symlink_to(index_target)
        result: subprocess.CompletedProcess[str] = self.run_install("reload")
        assert result.returncode != 0
        self.assert_restored()
        assert self.conf.is_symlink()
        assert self.index.is_symlink()
        assert config_target.read_text() == "# previous working config\n"
        assert index_target.read_text() == "previous index\n"
        reloads: list[dict[str, Any]] = [
            e for e in self.events() if e["args"] == ["reload-or-restart", "nginx"]
        ]
        assert len(reloads) == 2

    def test_success_removes_recovery_files_and_publishes_both_outputs(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode == 0, result.stderr
        assert "proxy_pass http://127.0.0.1:8080" in self.conf.read_text()
        assert "http://dashboard.example.internal" in self.index.read_text()
        assert not self.default.exists()
        assert self.default_target.exists()
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_apk_installs_nginx_before_validating_or_changing_luci(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt", nginx=False)
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode == 0, result.stderr
        events: list[dict[str, Any]] = self.events()
        commands: list[str] = [e["command"] for e in events]
        assert events[0] == {"command": "apk", "args": ["add", "nginx"]}
        first_uci_write: int = next(
            i
            for i, e in enumerate(events)
            if e["command"] == "uci" and e["args"][0] == "set"
        )
        assert commands.index("nginx") < first_uci_write
        assert commands.index("nginx") < commands.index("uhttpd-service")
        assert (
            json.loads((self.root / "uci.json").read_text())["uhttpd.main.listen_http"]
            == "127.0.0.1:8081"
        )

    def test_domain_named_default_keeps_its_generated_configuration(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        self.services.write_text("# domain: default\ndashboard 8080\n")
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode == 0, result.stderr
        assert not self.default.is_symlink()
        assert "server_name dashboard.default;" in self.default.read_text()
        assert self.default_target.read_text() == "# distribution default\n"

    def test_openwrt_bootstraps_missing_runtime_config_before_validation(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        assert (self.nginx / "uci.conf").is_symlink()
        assert not self.runtime_conf.exists()
        result: subprocess.CompletedProcess[str] = self.run_install(
            HORATIO_WAS_RUNNING="0"
        )
        assert result.returncode == 0, result.stderr
        assert self.runtime_conf.is_file()
        events: list[dict[str, Any]] = self.events()
        generation: int = next(
            i for i, e in enumerate(events) if e["command"] == "nginx-util"
        )
        validation: int = next(
            i for i, e in enumerate(events) if e["command"] == "nginx"
        )
        assert generation < validation
        assert events[validation]["args"] == ["-t", "-c", str(self.nginx / "uci.conf")]
        assert not any((e["args"] == ["start"] for e in events[:validation]))

    def test_openwrt_manual_mode_does_not_validate_stale_generated_config(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        self.runtime_conf.parent.mkdir(parents=True)
        self.runtime_conf.write_text("# stale runtime from previously enabled UCI\n")
        result: subprocess.CompletedProcess[str] = self.run_install(
            HORATIO_UCI_ENABLED="false"
        )
        assert result.returncode == 0, result.stderr
        validations: list[list[str]] = [
            e["args"] for e in self.events() if e["command"] == "nginx"
        ]
        assert validations == [["-t", "-c", str(self.nginx / "nginx.conf")]]

    def test_openwrt_runtime_generation_failure_preserves_service_configuration(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install("generate")
        assert result.returncode != 0
        self.assert_restored()
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert not any((e["command"] == "uhttpd-service" for e in self.events()))

    def test_openwrt_manual_config_still_validated_when_helper_fails(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install(
            "generate", HORATIO_UCI_ENABLED="false"
        )
        assert result.returncode == 0, result.stderr
        assert "validating manual configuration" in result.stderr
        assert ["-t", "-c", str(self.nginx / "nginx.conf")] in [
            e["args"] for e in self.events() if e["command"] == "nginx"
        ]

    def test_openwrt_absent_uci_flag_selects_manual_config(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install(
            HORATIO_UCI_ENABLED=""
        )
        assert result.returncode == 0, result.stderr
        assert [e["args"] for e in self.events() if e["command"] == "nginx"] == [
            ["-t", "-c", str(self.nginx / "nginx.conf")]
        ]

    def test_openwrt_waits_for_stop_and_http_readiness_before_committing(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install(
            HORATIO_STOP_DELAY="2", HORATIO_READY_DELAY="2"
        )
        assert result.returncode == 0, result.stderr
        events: list[dict[str, Any]] = self.events()
        probes: list[dict[str, Any]] = [e for e in events if e["command"] == "wget"]
        assert len(probes) == 3
        assert all((e["recovery_exists"] for e in probes))
        calls: list[list[str]] = [
            e["args"] for e in events if e["command"] == "nginx-service"
        ]
        stopping: int
        starting: int
        stopping, starting = calls.index(["stop"]), calls.index(["start"])
        assert calls[stopping + 1 : starting] == [["running"]] * 3
        last_probe: int = max(i for i, e in enumerate(events) if e["command"] == "wget")
        enabled: int = next(i for i, e in enumerate(events) if e["args"] == ["enable"])
        assert last_probe < enabled
        assert list(self.nginx.glob(".horatio-*")) == []

    def test_openwrt_async_nginx_failure_and_wrong_http_responder_both_roll_back(
        self: TestNginxInstaller,
        subtests: pytest.Subtests,
        tmp_path: Path,
    ) -> None:
        failure: str
        for failure in ("nginx-async", "wrong-index"):
            with subtests.test(failure=failure):
                # Each subcase gets its own initial service state and event log.
                case: TestNginxInstaller = TestNginxInstaller()
                case.prepare_root(tmp_path / failure)
                case.prepare("openwrt")
                result: subprocess.CompletedProcess[str] = case.run_install(
                    failure, HORATIO_WAS_RUNNING="0"
                )
                assert result.returncode != 0
                assert "did not serve the generated index" in result.stderr
                case.assert_restored()
                assert (case.root / "uci.json").read_text() == case.original_uci
                assert not any((e["args"] == ["enable"] for e in case.events()))

    def test_openwrt_async_uhttpd_failure_rolls_back_before_nginx_changes(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install("uhttpd-async")
        assert result.returncode != 0
        self.assert_restored()
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert [
            e["args"] for e in self.events() if e["command"] == "nginx-service"
        ] == [["running"]]

    def test_openwrt_async_restore_failure_keeps_recovery_files(
        self: TestNginxInstaller,
        subtests: pytest.Subtests,
        tmp_path: Path,
    ) -> None:
        service: str
        for service in ("nginx", "uhttpd"):
            with subtests.test(service=service):
                case: TestNginxInstaller = TestNginxInstaller()
                case.prepare_root(tmp_path / service)
                case.prepare("openwrt")
                result: subprocess.CompletedProcess[str] = case.run_install(
                    "nginx-async", HORATIO_FAIL_RESTORE=service
                )
                assert result.returncode != 0
                assert "recovery was incomplete" in result.stderr
                backups: list[Path] = list(case.nginx.glob(".horatio-*"))
                assert len(backups) == 1
                assert str(backups[0]) in result.stderr
                assert (
                    backups[0] / "config"
                ).read_text() == "# previous working config\n"
                assert (backups[0] / "index").read_text() == "previous index\n"
                assert (backups[0] / "uhttpd").read_text() == case.original_uci

    def test_missing_readiness_client_does_not_change_configuration(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        (self.bin / "wget").unlink()
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode != 0
        assert "wget is required" in result.stderr
        self.assert_restored()
        assert self.events() == []

    def test_missing_readiness_deadline_does_not_change_configuration(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        (self.bin / "timeout").unlink()
        result: subprocess.CompletedProcess[str] = self.run_install()
        assert result.returncode != 0
        assert "timeout is required" in result.stderr
        self.assert_restored()
        assert self.events() == []

    def test_missing_nginx_after_package_command_does_not_change_configuration(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt", nginx=False)
        result: subprocess.CompletedProcess[str] = self.run_install(
            HORATIO_PACKAGE_INSTALL="missing"
        )
        assert result.returncode != 0
        assert "nginx is unavailable" in result.stderr
        self.assert_restored()
        assert [e["command"] for e in self.events()] == ["apk"]
        assert (self.root / "uci.json").read_text() == self.original_uci

    def test_openwrt_validation_failure_never_changes_luci(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install("validate")
        assert result.returncode != 0
        self.assert_restored()
        assert [e["args"] for e in self.events() if e["command"] == "uci"] == [
            ["-q", "get", "nginx.global.uci_enable"]
        ]
        assert not any((e["command"] == "uhttpd-service" for e in self.events()))

    def test_failed_first_proxy_restart_restores_luci_and_stops_proxy(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install(
            "nginx-service", HORATIO_WAS_RUNNING="0"
        )
        assert result.returncode != 0
        self.assert_restored()
        assert (self.root / "uci.json").read_text() == self.original_uci
        service_calls: list[tuple[Any, Any]] = [
            (e["command"], e["args"]) for e in self.events()
        ]
        assert ("nginx-service", ["stop"]) in service_calls
        assert service_calls.count(("nginx-service", ["start"])) == 1
        restored_restart: dict[str, Any] = [
            e
            for e in self.events()
            if e["command"] == "uhttpd-service" and e["args"] == ["restart"]
        ][-1]
        assert restored_restart["config"] == json.loads(self.original_uci)

    def test_luci_restart_failure_restores_uci_without_restarting_nginx(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare("openwrt")
        result: subprocess.CompletedProcess[str] = self.run_install("uhttpd-service")
        assert result.returncode != 0
        self.assert_restored()
        assert (self.root / "uci.json").read_text() == self.original_uci
        assert [
            e["args"] for e in self.events() if e["command"] == "nginx-service"
        ] == [["running"]]

    def test_failed_restore_retains_backup_and_reports_its_location(
        self: TestNginxInstaller,
    ) -> None:
        self.prepare()
        # Fail validation, then fail the actual restore rename. The rejected
        # replacement must never be confused with the retained original backup.
        self.script.write_text(self.script.read_text().replace("nginx -t\n", "false\n"))
        (self.bin / "mv").unlink()
        (self.bin / "mv").symlink_to(self.mock)
        result: subprocess.CompletedProcess[str] = self.run_install("restore")
        assert result.returncode != 0
        backups: list[Path] = list(self.nginx.glob(".horatio-*"))
        assert len(backups) == 1
        assert str(backups[0]) in result.stderr
        assert (backups[0] / "config").read_text() == "# previous working config\n"
        assert self.index.read_text() == "previous index\n"
        assert self.default.is_symlink()
