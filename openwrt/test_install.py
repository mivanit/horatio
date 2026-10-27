"""Run the OpenWrt installer with temporary files and mocked system commands.

Usage: python3 openwrt/test_install.py
No network, package installation, service changes, or native modules are used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSET = b"// Chart.js test fixture\nwindow.Chart = {};\n"
ASSET_HASH = hashlib.sha256(ASSET).hexdigest()
MOCK = r'''#!/usr/bin/env python3
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
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="horatio-installer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "openwrt").mkdir()
        (self.root / "static").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "crontabs").mkdir()
        self.cron = self.root / "crontabs" / pwd.getpwuid(os.geteuid()).pw_name
        self.asset = self.root / "static/chart.umd.min.js"
        (self.root / "asset").write_bytes(ASSET)
        self.example = {"title": "Example", "ping_targets": [{"address": "8.8.8.8"}]}
        (self.root / "config.example.json").write_text(json.dumps(self.example))
        script = (ROOT / "openwrt/install.sh").read_text()
        # Redirect system paths to fixtures. The actual installer control flow,
        # checksum utility, temporary file and mv run.
        script = script.replace("/etc/init.d/uhttpd", "test-uhttpd-service")
        script = script.replace("/etc/init.d/cron", "test-cron-service")
        script = script.replace("/etc/crontabs", str(self.root / "crontabs"))
        script = re.sub(r'CHARTJS_SHA256="[a-f0-9]+"', f'CHARTJS_SHA256="{ASSET_HASH}"', script)
        self.script = self.root / "openwrt/install.sh"
        self.script.write_text(script)
        mock = self.bin / "mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("uci", "uhttpd", "apk", "wget", "crontab", "sleep", "ip",
                     "test-uhttpd-service", "test-cron-service"):
            (self.bin / name).symlink_to(mock)

    def run_install(self, mode="success", *, extra_env=None, uninstall=False):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   HORATIO_INSTALL_TEST=str(self.root), HORATIO_DOWNLOAD_MODE=mode,
                   HORATIO_UNINSTALL="1" if uninstall else "0")
        env.pop("LAN_IP", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["sh", str(self.script), *(["--uninstall"] if uninstall else [])], env=env, text=True,
                              capture_output=True, timeout=10)

    def events(self, command):
        path = self.root / "events"
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [entry for entry in entries if entry["command"] == command]

    def downloads(self):
        return [entry for entry in self.events("wget") if entry["args"][-1].startswith("https://")]

    def cron_writes(self):
        return [entry for entry in self.events("crontab") if "content" in entry]

    def test_download_failure_is_retried_and_config_precedes_restart(self):
        failed = self.run_install("failure")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.asset.exists())
        self.assertEqual(list((self.root / "static").glob(".chart.*")), [])
        self.assertEqual(self.events("test-uhttpd-service"), [])
        recovered = self.run_install()
        self.assertEqual(recovered.returncode, 0, recovered.stderr + recovered.stdout)
        self.assertEqual(self.asset.read_bytes(), ASSET)
        self.assertEqual(len(self.downloads()), 2)
        self.assertEqual(self.events("test-uhttpd-service")[0]["config"], self.example)
        self.assertTrue(all(entry["args"][1] != str(self.asset) for entry in self.downloads()))

    def test_existing_empty_or_partial_asset_is_repaired(self):
        for old in (b"", b"old partial download"):
            with self.subTest(old=old):
                self.asset.write_bytes(old)
                result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(self.asset.read_bytes(), ASSET)

    def test_checksum_mismatch_keeps_previous_asset_and_does_not_restart(self):
        previous = b"previous asset"
        self.asset.write_bytes(previous)
        result = self.run_install("mismatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.asset.read_bytes(), previous)
        self.assertEqual(list((self.root / "static").glob(".chart.*")), [])
        self.assertEqual(self.events("test-uhttpd-service"), [])

    def test_valid_asset_and_existing_config_are_preserved(self):
        self.asset.write_bytes(ASSET)
        custom = {"title": "My Router", "ping_targets": []}
        (self.root / "config.json").write_text(json.dumps(custom))
        result = self.run_install("failure")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.events("test-uhttpd-service")[0]["config"], custom)

    def test_pins_match_linux_installer(self):
        for key in ("CHARTJS_VERSION", "CHARTJS_SHA256"):
            pattern = rf'^{key}="([^"]+)"$'
            router = re.search(pattern, (ROOT / "openwrt/install.sh").read_text(), re.MULTILINE)
            linux = re.search(pattern, (ROOT / "install.sh").read_text(), re.MULTILINE)
            self.assertIsNotNone(router)
            self.assertIsNotNone(linux)
            self.assertEqual(router.group(1), linux.group(1))

    def test_lan_ip_override_skips_detection_and_strips_cidr(self):
        for address in ("192.168.50.1", "192.168.50.1/24"):
            with self.subTest(address=address):
                result = self.run_install(extra_env={"LAN_IP": address})
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn(["add_list", "uhttpd.dashboard.listen_http=192.168.50.1:8080"],
                              [event["args"] for event in self.events("uci")])
                self.assertNotIn(["get", "network.lan.ipaddr"],
                                 [event["args"] for event in self.events("uci")])
                self.assertEqual(self.events("ip"), [])
                self.assertEqual(self.events("wget")[-1]["args"][-1],
                                 "http://192.168.50.1:8080/api/4/load")

    def test_empty_override_uses_detected_lan_ip(self):
        result = self.run_install(extra_env={"LAN_IP": "", "HORATIO_DETECTED_LAN_IP": "192.168.3.1/24"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(["add_list", "uhttpd.dashboard.listen_http=192.168.3.1:8080"],
                      [event["args"] for event in self.events("uci")])
        self.assertEqual(self.events("ip"), [])

    def test_missing_uci_address_falls_back_to_non_loopback_interface(self):
        result = self.run_install(extra_env={"HORATIO_DETECTED_LAN_IP": ""})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(["add_list", "uhttpd.dashboard.listen_http=192.168.2.1:8080"],
                      [event["args"] for event in self.events("uci")])
        self.assertEqual(len(self.events("ip")), 1)

    def test_only_owned_cron_entries_are_replaced_or_removed(self):
        unrelated = ("0 1 * * * /usr/local/bin/dashboard-history-backup.sh\n"
                     "5 1 * * * echo dashboard-history\n"
                     "10 1 * * * /backup # dashboard-history-backup\n"
                     "15 1 * * * /ordinary-job\n")
        owned = ("0 3,15 * * * /old-job # dashboard-history\n"
                 "0 3,15 * * * /duplicate-job # dashboard-history \t\n")
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                self.cron.write_text(unrelated + owned)
                (self.root / "events").unlink(missing_ok=True)
                result = self.run_install(uninstall=uninstall)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                writes = self.cron_writes()
                self.assertEqual(len(writes), 1)
                final = self.cron.read_text()
                self.assertEqual(writes[0]["content"], final)
                if uninstall:
                    self.assertEqual(final, unrelated)
                else:
                    expected = ("0 3,15 * * * wget -qO /dev/null --post-data='' "
                                "http://192.168.1.1:8080/api/history/record # dashboard-history\n")
                    self.assertEqual(final, unrelated + expected)

    def test_missing_and_empty_cron_tables_are_supported(self):
        for exists in (False, True):
            for uninstall in (False, True):
                with self.subTest(exists=exists, uninstall=uninstall):
                    self.cron.unlink(missing_ok=True)
                    if exists:
                        self.cron.write_text("")
                    result = self.run_install(uninstall=uninstall)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    self.assertEqual(self.cron.read_text().count("# dashboard-history"),
                                     0 if uninstall else 1)

    def test_failed_cron_read_never_replaces_existing_jobs(self):
        previous = "0 1 * * * /backup\n0 3,15 * * * /record # dashboard-history\n"
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                self.cron.write_text(previous)
                result = self.run_install(uninstall=uninstall, extra_env={
                    "HORATIO_FAIL_COMMAND": json.dumps(["crontab", "-c", str(self.cron.parent), "-l"]),
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.cron.read_text(), previous)
                self.assertEqual(self.cron_writes(), [])
                self.assertNotIn("Dashboard removed.", result.stdout)

    def test_broken_cron_symlink_is_not_treated_as_an_empty_table(self):
        self.cron.symlink_to(self.root / "missing-table")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.cron.is_symlink())
        self.assertEqual(self.cron_writes(), [])

    def test_failed_or_silently_ignored_cron_write_is_reported(self):
        previous = "0 1 * * * /backup\n0 3,15 * * * /record # dashboard-history\n"
        for mode in ("failure", "silent"):
            for uninstall in (False, True):
                with self.subTest(mode=mode, uninstall=uninstall):
                    self.cron.write_text(previous)
                    (self.root / "events").unlink(missing_ok=True)
                    result = self.run_install(uninstall=uninstall,
                                              extra_env={"HORATIO_CRON_WRITE_MODE": mode})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.cron.read_text(), previous)
                    self.assertEqual(len(self.cron_writes()), 1)
                    self.assertNotIn("Dashboard removed.", result.stdout)
                    self.assertNotIn("Dashboard is running!", result.stdout)

    def test_uninstall_stops_on_uci_or_service_failure_before_removing_cron(self):
        previous = "0 3,15 * * * /record # dashboard-history\n"
        for command in (["uci", "show", "uhttpd"], ["uci", "delete", "uhttpd.dashboard"],
                        ["uci", "commit", "uhttpd"], ["test-uhttpd-service", "restart"]):
            with self.subTest(command=command):
                self.cron.write_text(previous)
                result = self.run_install(uninstall=True,
                                          extra_env={"HORATIO_FAIL_COMMAND": json.dumps(command)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("injected command failure", result.stderr)
                self.assertNotIn("Dashboard removed.", result.stdout)
                self.assertEqual(self.cron.read_text(), previous)
                self.assertEqual(self.cron_writes(), [])

    def test_uninstall_missing_section_is_repeatable_and_preserves_data(self):
        previous = "0 1 * * * /backup\n"
        self.cron.write_text(previous)
        config = self.root / "config.json"
        config.write_text('{"title": "My Router"}\n')
        history = self.root / "logs/history.jsonl"
        history.parent.mkdir()
        history.write_text('{"ts": 42}\n')
        for _ in range(2):
            result = self.run_install(uninstall=True, extra_env={"HORATIO_SECTION_MISSING": "1"})
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Dashboard removed.", result.stdout)
            self.assertEqual(self.cron.read_text(), previous)
            self.assertEqual(config.read_text(), '{"title": "My Router"}\n')
            self.assertEqual(history.read_text(), '{"ts": 42}\n')
        self.assertNotIn(["delete", "uhttpd.dashboard"], [event["args"] for event in self.events("uci")])

    def test_install_reports_cron_service_failure(self):
        result = self.run_install(extra_env={
            "HORATIO_FAIL_COMMAND": json.dumps(["test-cron-service", "restart"]),
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Dashboard is running!", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
