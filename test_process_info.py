"""Process argument privacy checks: python -m unittest test_process_info.

Uses real /proc data for Python's inline-code mode and pure argv fixtures for
other interpreters, so Ruby, Perl, and PHP do not need to be installed.
"""

import subprocess
import sys
import unittest

import server


SECRET = "HORATIO_TEST_SECRET_MARKER"


class ProcessInfoTests(unittest.TestCase):
    def test_script_and_module_positions(self) -> None:
        cases = [
            (["/usr/bin/python3", "-u", "-m", "http.server", "--token", SECRET],
             "http.server", "/usr/bin/python3 -m http.server"),
            (["python3.13", "-I", "-B", "app.py", "-m", SECRET], "app.py", "python3.13 app.py"),
            (["python", "-W", "ignore", "-X", "dev", "/srv/my app.py", SECRET],
             "my app.py", "python /srv/my app.py"),
            (["python", "-Wignore", "-Xdev", "-m", "package.module", SECRET],
             "package.module", "python -m package.module"),
            (["python", "--", "-script.py", SECRET], "-script.py", "python -script.py"),
            (["pypy3", "script.py", SECRET], "script.py", "pypy3 script.py"),
            (["node", "--no-warnings", "/srv/app.js", "--eval", SECRET], "app.js", "node /srv/app.js"),
            (["nodejs", "--", "app.js", SECRET], "app.js", "nodejs app.js"),
            (["ruby3.1", "-w", "/srv/app.rb", SECRET], "app.rb", "ruby3.1 /srv/app.rb"),
            (["perl", "-w", "/srv/app.pl", SECRET], "app.pl", "perl /srv/app.pl"),
            (["php8.2", "-n", "-f", "/srv/app.php", SECRET], "app.php", "php8.2 /srv/app.php"),
            (["php", "/srv/app.php", SECRET], "app.php", "php /srv/app.php"),
        ]
        for argv, name, path in cases:
            with self.subTest(argv=argv):
                self.assertEqual(server._sanitize_process_argv(argv), {"name": name, "path": path})

    def test_inline_code_and_unknown_option_operands_are_private(self) -> None:
        code = f'token="{SECRET}"'
        cases = [
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
        for argv in cases:
            with self.subTest(argv=argv):
                info = server._sanitize_process_argv(argv)
                self.assertEqual(info, {"name": argv[0], "path": argv[0]})
                self.assertNotIn(SECRET, info["name"])
                self.assertNotIn(SECRET, info["path"])

    def test_real_python_inline_code_stays_out_of_live_and_history_names(self) -> None:
        # Empty option operands must survive /proc splitting; otherwise -c can
        # be mistaken for the preceding option's operand and expose its body.
        for options in ([], ["-W", ""]):
            with self.subTest(options=options):
                child = subprocess.Popen(
                    [sys.executable, *options, "-c",
                     f'import time; token="{SECRET}"; print("ready", flush=True); time.sleep(30)'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    # /proc can briefly expose an empty cmdline during exec.
                    # Inspect a running interpreter, not that startup window.
                    self.assertEqual(child.stdout.readline(), "ready\n")
                    info = server.get_sanitized_process_info(child.pid)
                    self.assertNotIn(SECRET, info["name"])
                    self.assertNotIn(SECRET, info["path"])
                    self.assertEqual(info["path"], sys.executable)
                    rows = server.build_process_records(
                        [{"pid": child.pid, "name": "python", "cpu_percent": 1, "memory_info": {"rss": 1024}}],
                        [],
                        1,
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["name"], info["name"])
                    self.assertNotIn(SECRET, rows[0]["name"])
                finally:
                    child.terminate()
                    child.wait(timeout=5)
                    child.stdout.close()


if __name__ == "__main__":
    unittest.main()
