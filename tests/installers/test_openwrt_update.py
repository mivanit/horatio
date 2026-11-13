"""`openwrt/install.sh --update` on a checkout copied over without .git: the
release tarball path, with wget and jsonfilter mocked and a local tarball.

The release's own installer is a stub that leaves a marker, standing in for
"the updated installer ran"; nothing touches uci, uhttpd or the network.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT

MOCK: str = r"""#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["HORATIO_UPDATE_TEST"])
if name == "jsonfilter":
    assert args == ["-e", "@.tag_name"], args
    print(json.loads(sys.stdin.read())["tag_name"])
    sys.exit(0)
with (root / "wget.log").open("a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("HORATIO_WGET_FAIL") == "1":
    sys.exit(4)
if args[0] == "-qO-":
    print(json.dumps({"tag_name": os.environ["HORATIO_LATEST_TAG"]}))
else:
    shutil.copy(root / "release.tar.gz", args[args.index("-qO") + 1])
"""
STUB: str = '#!/bin/sh\necho ran > "$(dirname "$0")/../ran"\n'


class Router:
    def __init__(self: Router, tmp_path: Path) -> None:
        self.tmp: Path = tmp_path
        self.box: Path = tmp_path / "horatio"
        (self.box / "openwrt").mkdir(parents=True)
        (self.box / "openwrt/install.sh").write_text(
            (ROOT / "openwrt/install.sh").read_text()
        )
        (self.box / "VERSION").write_text("0.1.0\n")
        (self.box / "README.md").write_text("old\n")
        (self.box / "www").mkdir()
        (self.box / "www/config.json").write_text('{"title": "mine"}\n')
        (self.box / "logs").mkdir()
        (self.box / "logs/history.jsonl").write_text("{}\n")
        self.bin: Path = tmp_path / "bin"
        self.bin.mkdir()
        mock: Path = self.bin / "mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("wget", "jsonfilter"):
            (self.bin / name).symlink_to(mock)

    def release(
        self: Router, version: str, extra: dict[str, str] | None = None
    ) -> None:
        """The GitHub archive of v<version>: one horatio-<version>/ directory."""
        files: dict[str, str] = {
            "VERSION": version + "\n",
            "README.md": "new\n",
            "openwrt/install.sh": STUB,
            **(extra or {}),
        }
        with tarfile.open(self.tmp / "release.tar.gz", "w:gz") as tar:
            # What a BusyBox tar without pax support leaves of GitHub's global
            # header: a file that sorts after horatio-<version>/.
            stray: tarfile.TarInfo = tarfile.TarInfo("pax_global_header")
            stray.size = 3
            tar.addfile(stray, io.BytesIO(b"abc"))
            for path, text in files.items():
                data: bytes = text.encode()
                info: tarfile.TarInfo = tarfile.TarInfo(f"horatio-{version}/{path}")
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))

    def update(
        self: Router, *args: str, latest: str = "v0.2.0", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.box / "openwrt/install.sh"), "--update", *args],
            env=dict(
                os.environ,
                PATH=f"{self.bin}:{os.environ['PATH']}",
                HORATIO_UPDATE_TEST=str(self.tmp),
                HORATIO_LATEST_TAG=latest,
                **env,
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def downloads(self: Router) -> list[Any]:
        log: Path = self.tmp / "wget.log"
        if not log.exists():
            return []
        return [json.loads(line)[-1] for line in log.read_text().splitlines()]

    def unchanged(self: Router) -> bool:
        return (
            (self.box / "VERSION").read_text() == "0.1.0\n"
            and (self.box / "README.md").read_text() == "old\n"
            and not (self.box / "ran").exists()
            and not list(self.box.glob(".update.*"))
        )


@pytest.fixture
def router(tmp_path: Path) -> Router:
    return Router(tmp_path)


def test_newest_release_replaces_code_and_keeps_local_state(router: Router) -> None:
    router.release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update()
    assert result.returncode == 0, result.stdout + result.stderr
    assert router.downloads() == [
        "https://api.github.com/repos/mivanit/horatio/releases/latest",
        "https://github.com/mivanit/horatio/archive/refs/tags/v0.2.0.tar.gz",
    ]
    assert (router.box / "VERSION").read_text() == "0.2.0\n"
    assert (router.box / "README.md").read_text() == "new\n"
    assert (router.box / "ran").exists(), "the release's installer ran"
    assert (router.box / "www/config.json").read_text() == '{"title": "mine"}\n'
    assert (router.box / "logs/history.jsonl").read_text() == "{}\n"
    assert not list(router.box.glob(".update.*")), "the download is cleaned up"
    assert "v0.1.0 -> v0.2.0" in result.stdout


def test_named_release_skips_the_api(router: Router) -> None:
    router.release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update("0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert router.downloads() == [
        "https://github.com/mivanit/horatio/archive/refs/tags/v0.2.0.tar.gz"
    ]


def test_already_at_newest(router: Router) -> None:
    result: subprocess.CompletedProcess[str] = router.update(latest="v0.1.0")
    assert result.returncode == 0 and "Already at v0.1.0" in result.stdout
    assert len(router.downloads()) == 1 and router.unchanged()


@pytest.mark.parametrize(
    "case,expect",
    [
        ("mismatch", "does not contain VERSION 0.2.0"),
        ("local-state", "contains config.json"),
        ("offline", "ca-bundle"),
        ("downgrade", "older than this checkout"),
    ],
)
def test_refusals_change_nothing(router: Router, case: str, expect: str) -> None:
    env: dict[str, str] = {}
    latest: str = "v0.2.0"
    if case == "mismatch":
        router.release("0.2.1")
    elif case == "local-state":
        router.release("0.2.0", {"config.json": "{}"})
    elif case == "offline":
        env["HORATIO_WGET_FAIL"] = "1"
    else:
        latest = "v0.0.9"
    result: subprocess.CompletedProcess[str] = router.update(latest=latest, **env)
    assert result.returncode == 1 and expect in result.stdout, (
        result.stdout + result.stderr
    )
    assert router.unchanged()
