"""Exercise nginx installation and recovery using isolated files and services.

Run with python3 nginx/test_install.py; no privileges, network or nginx required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/python3
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
        state = {"phase": "candidate" if candidate else "rollback", "running": True, "checks": 0}
        if candidate and failure == name:
            state["running"] = False
            state_file.write_text(json.dumps(state))
            sys.exit(1)
    state_file.write_text(json.dumps(state))
elif name == "wget":
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
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="horatio-nginx-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.nginx = self.root / "etc/nginx"
        for directory in ("sites-enabled", "sites-available", "conf.d"):
            (self.nginx / directory).mkdir(parents=True)
        (self.root / "init.d").mkdir()
        self.mock = self.root / "mock"
        self.mock.write_text(MOCK)
        self.mock.chmod(0o755)
        # A closed PATH allows a real missing-nginx/apk-only OpenWrt scenario,
        # regardless of commands installed on the test host.
        for name in ("awk", "basename", "chmod", "cp", "dirname", "grep", "head",
                     "mkdir", "mktemp", "mv", "rm", "sed", "timeout", "tr"):
            command = shutil.which(name)
            self.assertIsNotNone(command, name)
            (self.bin / name).symlink_to(command)
        self.env = dict(os.environ, PATH=str(self.bin), HORATIO_NGINX_TEST_ROOT=str(self.root),
                        HORATIO_REAL_MV=shutil.which("mv"))
        script = (ROOT / "nginx/install.sh").read_text()
        script = script.replace("/etc/nginx", str(self.nginx))
        script = script.replace("/etc/init.d", str(self.root / "init.d"))
        script = script.replace("/var/www", str(self.root / "var/www"))
        script = script.replace("/var/log/nginx", str(self.root / "var/log/nginx"))
        script = script.replace("/var/lib/nginx", str(self.root / "var/lib/nginx"))
        script = script.replace('INDEX_DIR="/www/', f'INDEX_DIR="{self.root}/www/')
        self.script = self.root / "install.sh"
        self.script.write_text(script)
        self.services = self.root / "host.services"
        self.services.write_text("# domain: example.internal\ndashboard 8080\n")
        self.original_uci = json.dumps({
            "uhttpd.main.listen_http": ["0.0.0.0:80", "[::]:80"],
            "uhttpd.main.listen_https": ["0.0.0.0:443", "[::]:443"],
            "uhttpd.custom.option": "keep this",
        }, indent=2)
        (self.root / "uci.json").write_text(self.original_uci)

    def prepare(self, platform="ubuntu", *, nginx=True):
        if platform == "ubuntu":
            (self.bin / "systemctl").symlink_to(self.mock)
            self.conf = self.nginx / "sites-enabled/example-internal"
            self.index = self.root / "var/www/example-internal/index.html"
        else:
            for command in ("uci", "apk", "nginx-util", "wget", "sleep"):
                (self.bin / command).symlink_to(self.mock)
            for service in ("nginx", "uhttpd"):
                (self.root / "init.d" / service).symlink_to(self.mock)
            self.runtime_conf = self.root / "var/lib/nginx/uci.conf"
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
        self.default_target = self.nginx / "sites-available/default"
        self.default_target.write_text("# distribution default\n")
        self.default = self.nginx / "sites-enabled/default"
        self.default.symlink_to("../sites-available/default")

    def run_install(self, failure="", **env):
        return subprocess.run(["/bin/sh", str(self.script), str(self.services)],
                              env=dict(self.env, HORATIO_NGINX_FAIL=failure, **env),
                              capture_output=True, text=True, timeout=10)

    def events(self):
        path = self.root / "events"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_restored(self):
        self.assertEqual(self.conf.read_text(), "# previous working config\n")
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.index.read_text(), "previous index\n")
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.readlink(self.default), "../sites-available/default")
        self.assertEqual(self.default_target.read_text(), "# distribution default\n")
        self.assertEqual(list(self.nginx.glob(".horatio-*")), [])

    def test_rejected_config_restores_files_and_default_symlink(self):
        self.prepare()
        result = self.run_install("validate")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertFalse(any(e["args"][0] == "reload-or-restart" for e in self.events()))

    def test_failed_reload_restores_symlinks_without_changing_their_targets(self):
        self.prepare()
        config_target, index_target = self.root / "config-source", self.root / "index-source"
        self.conf.rename(config_target)
        self.index.rename(index_target)
        self.conf.symlink_to(config_target)
        self.index.symlink_to(index_target)
        result = self.run_install("reload")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertTrue(self.conf.is_symlink())
        self.assertTrue(self.index.is_symlink())
        self.assertEqual(config_target.read_text(), "# previous working config\n")
        self.assertEqual(index_target.read_text(), "previous index\n")
        reloads = [e for e in self.events() if e["args"] == ["reload-or-restart", "nginx"]]
        self.assertEqual(len(reloads), 2)

    def test_success_removes_recovery_files_and_publishes_both_outputs(self):
        self.prepare()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("proxy_pass http://127.0.0.1:8080", self.conf.read_text())
        self.assertIn("http://dashboard.example.internal", self.index.read_text())
        self.assertFalse(self.default.exists())
        self.assertTrue(self.default_target.exists())
        self.assertEqual(list(self.nginx.glob(".horatio-*")), [])

    def test_apk_installs_nginx_before_validating_or_changing_luci(self):
        self.prepare("openwrt", nginx=False)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        commands = [e["command"] for e in events]
        self.assertEqual(events[0], {"command": "apk", "args": ["add", "nginx"]})
        first_uci_write = next(i for i, e in enumerate(events) if e["command"] == "uci" and e["args"][0] == "set")
        self.assertLess(commands.index("nginx"), first_uci_write)
        self.assertLess(commands.index("nginx"), commands.index("uhttpd-service"))
        self.assertEqual(json.loads((self.root / "uci.json").read_text())["uhttpd.main.listen_http"],
                         "127.0.0.1:8081")

    def test_domain_named_default_keeps_its_generated_configuration(self):
        self.prepare()
        self.services.write_text("# domain: default\ndashboard 8080\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.default.is_symlink())
        self.assertIn("server_name dashboard.default;", self.default.read_text())
        self.assertEqual(self.default_target.read_text(), "# distribution default\n")

    def test_openwrt_bootstraps_missing_runtime_config_before_validation(self):
        self.prepare("openwrt")
        self.assertTrue((self.nginx / "uci.conf").is_symlink())
        self.assertFalse(self.runtime_conf.exists())
        result = self.run_install(HORATIO_WAS_RUNNING="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.runtime_conf.is_file())
        events = self.events()
        generation = next(i for i, e in enumerate(events) if e["command"] == "nginx-util")
        validation = next(i for i, e in enumerate(events) if e["command"] == "nginx")
        self.assertLess(generation, validation)
        self.assertEqual(events[validation]["args"], ["-t", "-c", str(self.nginx / "uci.conf")])
        self.assertFalse(any(e["args"] == ["start"] for e in events[:validation]))

    def test_openwrt_manual_mode_does_not_validate_stale_generated_config(self):
        self.prepare("openwrt")
        self.runtime_conf.parent.mkdir(parents=True)
        self.runtime_conf.write_text("# stale runtime from previously enabled UCI\n")
        result = self.run_install(HORATIO_UCI_ENABLED="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        validations = [e["args"] for e in self.events() if e["command"] == "nginx"]
        self.assertEqual(validations, [["-t", "-c", str(self.nginx / "nginx.conf")]])

    def test_openwrt_runtime_generation_failure_preserves_service_configuration(self):
        self.prepare("openwrt")
        result = self.run_install("generate")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual((self.root / "uci.json").read_text(), self.original_uci)
        self.assertFalse(any(e["command"] == "uhttpd-service" for e in self.events()))

    def test_openwrt_manual_config_still_validated_when_helper_fails(self):
        self.prepare("openwrt")
        result = self.run_install("generate", HORATIO_UCI_ENABLED="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validating manual configuration", result.stderr)
        self.assertIn(["-t", "-c", str(self.nginx / "nginx.conf")],
                      [e["args"] for e in self.events() if e["command"] == "nginx"])

    def test_openwrt_absent_uci_flag_selects_manual_config(self):
        self.prepare("openwrt")
        result = self.run_install(HORATIO_UCI_ENABLED="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["args"] for e in self.events() if e["command"] == "nginx"],
                         [["-t", "-c", str(self.nginx / "nginx.conf")]])

    def test_openwrt_waits_for_stop_and_http_readiness_before_committing(self):
        self.prepare("openwrt")
        result = self.run_install(HORATIO_STOP_DELAY="2", HORATIO_READY_DELAY="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        probes = [e for e in events if e["command"] == "wget"]
        self.assertEqual(len(probes), 3)
        self.assertTrue(all(e["recovery_exists"] for e in probes))
        calls = [e["args"] for e in events if e["command"] == "nginx-service"]
        stopping, starting = calls.index(["stop"]), calls.index(["start"])
        self.assertEqual(calls[stopping + 1:starting], [["running"]] * 3)
        last_probe = max(i for i, e in enumerate(events) if e["command"] == "wget")
        enabled = next(i for i, e in enumerate(events) if e["args"] == ["enable"])
        self.assertLess(last_probe, enabled)
        self.assertEqual(list(self.nginx.glob(".horatio-*")), [])

    def test_openwrt_async_nginx_failure_and_wrong_http_responder_both_roll_back(self):
        for failure in ("nginx-async", "wrong-index"):
            with self.subTest(failure=failure):
                # Each subcase gets its own initial service state and event log.
                case = InstallerTests()
                case.setUp()
                try:
                    case.prepare("openwrt")
                    result = case.run_install(failure, HORATIO_WAS_RUNNING="0")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("did not serve the generated index", result.stderr)
                    case.assert_restored()
                    self.assertEqual((case.root / "uci.json").read_text(), case.original_uci)
                    self.assertFalse(any(e["args"] == ["enable"] for e in case.events()))
                finally:
                    case.doCleanups()

    def test_openwrt_async_uhttpd_failure_rolls_back_before_nginx_changes(self):
        self.prepare("openwrt")
        result = self.run_install("uhttpd-async")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual((self.root / "uci.json").read_text(), self.original_uci)
        self.assertEqual([e["args"] for e in self.events() if e["command"] == "nginx-service"],
                         [["running"]])

    def test_openwrt_async_restore_failure_keeps_recovery_files(self):
        for service in ("nginx", "uhttpd"):
            with self.subTest(service=service):
                case = InstallerTests()
                case.setUp()
                try:
                    case.prepare("openwrt")
                    result = case.run_install("nginx-async", HORATIO_FAIL_RESTORE=service)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("recovery was incomplete", result.stderr)
                    backups = list(case.nginx.glob(".horatio-*"))
                    self.assertEqual(len(backups), 1)
                    self.assertIn(str(backups[0]), result.stderr)
                    self.assertEqual((backups[0] / "config").read_text(), "# previous working config\n")
                    self.assertEqual((backups[0] / "index").read_text(), "previous index\n")
                    self.assertEqual((backups[0] / "uhttpd").read_text(), case.original_uci)
                finally:
                    case.doCleanups()

    def test_missing_readiness_client_does_not_change_configuration(self):
        self.prepare("openwrt")
        (self.bin / "wget").unlink()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wget is required", result.stderr)
        self.assert_restored()
        self.assertEqual(self.events(), [])

    def test_missing_readiness_deadline_does_not_change_configuration(self):
        self.prepare("openwrt")
        (self.bin / "timeout").unlink()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timeout is required", result.stderr)
        self.assert_restored()
        self.assertEqual(self.events(), [])

    def test_missing_nginx_after_package_command_does_not_change_configuration(self):
        self.prepare("openwrt", nginx=False)
        result = self.run_install(HORATIO_PACKAGE_INSTALL="missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nginx is unavailable", result.stderr)
        self.assert_restored()
        self.assertEqual([e["command"] for e in self.events()], ["apk"])
        self.assertEqual((self.root / "uci.json").read_text(), self.original_uci)

    def test_openwrt_validation_failure_never_changes_luci(self):
        self.prepare("openwrt")
        result = self.run_install("validate")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual([e["args"] for e in self.events() if e["command"] == "uci"],
                         [["-q", "get", "nginx.global.uci_enable"]])
        self.assertFalse(any(e["command"] == "uhttpd-service" for e in self.events()))

    def test_failed_first_proxy_restart_restores_luci_and_stops_proxy(self):
        self.prepare("openwrt")
        result = self.run_install("nginx-service", HORATIO_WAS_RUNNING="0")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual((self.root / "uci.json").read_text(), self.original_uci)
        service_calls = [(e["command"], e["args"]) for e in self.events()]
        self.assertIn(("nginx-service", ["stop"]), service_calls)
        self.assertEqual(service_calls.count(("nginx-service", ["start"])), 1)
        restored_restart = [e for e in self.events() if e["command"] == "uhttpd-service" and e["args"] == ["restart"]][-1]
        self.assertEqual(restored_restart["config"], json.loads(self.original_uci))

    def test_luci_restart_failure_restores_uci_without_restarting_nginx(self):
        self.prepare("openwrt")
        result = self.run_install("uhttpd-service")
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual((self.root / "uci.json").read_text(), self.original_uci)
        self.assertEqual([e["args"] for e in self.events() if e["command"] == "nginx-service"],
                         [["running"]])

    def test_failed_restore_retains_backup_and_reports_its_location(self):
        self.prepare()
        # Fail validation, then fail the actual restore rename. The rejected
        # replacement must never be confused with the retained original backup.
        self.script.write_text(self.script.read_text().replace("nginx -t\n", "false\n"))
        (self.bin / "mv").unlink()
        (self.bin / "mv").symlink_to(self.mock)
        result = self.run_install("restore")
        self.assertNotEqual(result.returncode, 0)
        backups = list(self.nginx.glob(".horatio-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), result.stderr)
        self.assertEqual((backups[0] / "config").read_text(), "# previous working config\n")
        self.assertEqual(self.index.read_text(), "previous index\n")
        self.assertTrue(self.default.is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
