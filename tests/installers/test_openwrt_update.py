"""`lua/install-openwrt.sh --update`: wget is mocked to serve main's VERSION and a
local release tarball.

The release's own installer is a stub that leaves a marker, standing in for
"the updated installer ran"; nothing touches uci, uhttpd or the network.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import zlib
from pathlib import Path

import pytest

from tests.support import ROOT

MOCK: str = r"""#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["HORATIO_UPDATE_TEST"])
with (root / "wget.log").open("a") as log:
    log.write(args[-1] + "\n")
if os.environ.get("HORATIO_WGET_FAIL") == "1":
    sys.exit(4)
if args[0] == "-qO-":
    print(os.environ["HORATIO_MAIN_VERSION"])
else:
    shutil.copy(root / "release.tar.gz", args[args.index("-qO") + 1])
"""
STUB: str = '#!/bin/sh\necho ran > "$(dirname "$0")/../ran"\n'


class Router:
    def __init__(self: Router, tmp_path: Path) -> None:
        self.tmp: Path = tmp_path
        self.box: Path = tmp_path / "horatio"
        (self.box / "lua").mkdir(parents=True)
        (self.box / "lua/install-openwrt.sh").write_text(
            (ROOT / "lua/install-openwrt.sh").read_text()
        )
        (self.box / "VERSION").write_text("0.1.0\n")
        (self.box / "www").mkdir()
        (self.box / "www/config.json").write_text('{"title": "mine"}\n')
        self.bin: Path = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "wget").write_text(MOCK)
        (self.bin / "wget").chmod(0o755)

    def release(
        self: Router, version: str, installer: str = "lua/install-openwrt.sh"
    ) -> None:
        """The GitHub archive of v<version>: one horatio-<version>/ directory."""
        with tarfile.open(self.tmp / "release.tar.gz", "w:gz") as tar:
            for path, text in (
                ("VERSION", version + "\n"),
                (installer, STUB),
                *[
                    (legacy, "#!/bin/sh\nexit 99\n")
                    for legacy in ("lua/install.sh", "openwrt/install.sh")
                    if installer == "lua/install-openwrt.sh"
                ],
            ):
                data: bytes = text.encode()
                info: tarfile.TarInfo = tarfile.TarInfo(f"horatio-{version}/{path}")
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))

    def cut_release(self: Router, version: str) -> None:
        """A download of v<version> that stopped partway: the gzip stream ends
        after VERSION and the installer, before the archive's last file, at a
        point where the unpacked part is still a well-formed tar."""
        raw: io.BytesIO = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for path, text in (
                ("VERSION", version + "\n"),
                ("lua/install-openwrt.sh", STUB),
                ("static/app.js", "// the part that never arrived\n"),
            ):
                data: bytes = text.encode()
                info: tarfile.TarInfo = tarfile.TarInfo(f"horatio-{version}/{path}")
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
        with tarfile.open(fileobj=io.BytesIO(raw.getvalue())) as tar:
            keep: int = tar.getmembers()[2].offset
        packer = zlib.compressobj(wbits=31)
        (self.tmp / "release.tar.gz").write_bytes(
            packer.compress(raw.getvalue()[:keep]) + packer.flush(zlib.Z_FULL_FLUSH)
        )

    def update(
        self: Router, *args: str, main: str = "0.2.0", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.box / "lua/install-openwrt.sh"), "--update", *args],
            env=dict(
                os.environ,
                PATH=f"{self.bin}:{os.environ['PATH']}",
                HORATIO_UPDATE_TEST=str(self.tmp),
                HORATIO_MAIN_VERSION=main,
                **env,
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def downloads(self: Router) -> list[str]:
        log: Path = self.tmp / "wget.log"
        return log.read_text().splitlines() if log.exists() else []

    def unchanged(self: Router) -> bool:
        return (
            (self.box / "VERSION").read_text() == "0.1.0\n"
            and not (self.box / "ran").exists()
            and not list(self.box.glob(".update.*"))
        )


@pytest.mark.parametrize("installer", ["lua/install.sh", "openwrt/install.sh"])
def test_update_can_run_an_older_release_installer(
    tmp_path: Path, installer: str
) -> None:
    router = Router(tmp_path)
    router.release("0.2.0", installer=installer)
    result = router.update("v0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (router.box / "ran").read_text() == "ran\n"


def test_missing_release_installer_does_not_modify_checkout(tmp_path: Path) -> None:
    router = Router(tmp_path)
    router.release("0.2.0", installer="unrelated.sh")
    result = router.update("v0.2.0")
    assert (
        result.returncode != 0 and "no Lua installer" in result.stdout + result.stderr
    )
    assert (router.box / "VERSION").read_text() == "0.1.0\n"
    assert not (router.box / "unrelated.sh").exists()


@pytest.mark.parametrize("entrypoint", ["lua/install.sh", "openwrt/install.sh"])
def test_legacy_install_path_delegates_to_new_backend(
    tmp_path: Path, entrypoint: str
) -> None:
    # Older deployed updaters re-exec these paths after copying a new release.
    old = tmp_path / entrypoint
    new = tmp_path / "lua/install-openwrt.sh"
    old.parent.mkdir(parents=True, exist_ok=True)
    new.parent.mkdir(parents=True, exist_ok=True)
    old.write_text((ROOT / entrypoint).read_text())
    new.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/../args"\n')
    subprocess.run(["sh", str(old), "--update", "v0.2.0"], check=True)
    assert (tmp_path / "args").read_text().splitlines() == ["--update", "v0.2.0"]


@pytest.fixture
def router(tmp_path: Path) -> Router:
    return Router(tmp_path)


def test_newest_release_replaces_code_and_keeps_config(router: Router) -> None:
    router.release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update()
    assert result.returncode == 0, result.stdout + result.stderr
    assert router.downloads() == [
        "https://raw.githubusercontent.com/mivanit/horatio/main/VERSION",
        "https://codeload.github.com/mivanit/horatio/tar.gz/refs/tags/v0.2.0",
    ]
    assert (router.box / "VERSION").read_text() == "0.2.0\n"
    assert (router.box / "ran").exists(), "the release's installer ran"
    assert (router.box / "www/config.json").read_text() == '{"title": "mine"}\n'
    assert not list(router.box.glob(".update.*")), "the download is cleaned up"
    assert "To go back: " in result.stdout and "--update v0.1.0" in result.stdout


def test_named_release_is_downloaded_directly(router: Router) -> None:
    router.release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update("v0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert router.downloads() == [
        "https://codeload.github.com/mivanit/horatio/tar.gz/refs/tags/v0.2.0"
    ]
    assert (router.box / "VERSION").read_text() == "0.2.0\n"


def test_already_at_newest(router: Router) -> None:
    result: subprocess.CompletedProcess[str] = router.update(main="0.1.0")
    assert result.returncode == 0 and "Already at v0.1.0" in result.stdout
    assert router.unchanged()


@pytest.mark.parametrize("case", ["offline", "wrong-download"])
def test_failures_change_nothing(router: Router, case: str) -> None:
    env: dict[str, str] = {}
    if case == "offline":
        env["HORATIO_WGET_FAIL"] = "1"
    else:
        router.release("0.2.1")  # not the v0.2.0 that was asked for
    result: subprocess.CompletedProcess[str] = router.update(**env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert router.unchanged()


def test_naming_the_current_version_reinstalls_it(router: Router) -> None:
    # The way back after a copy that failed partway: VERSION still says 0.1.0.
    router.release("0.1.0")
    result: subprocess.CompletedProcess[str] = router.update("v0.1.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Already at" not in result.stdout
    assert router.downloads() == [
        "https://codeload.github.com/mivanit/horatio/tar.gz/refs/tags/v0.1.0"
    ]
    assert (router.box / "ran").exists(), "the release's installer ran"
    assert (router.box / "VERSION").read_text() == "0.1.0\n"
    assert (router.box / "www/config.json").read_text() == '{"title": "mine"}\n'
    assert not list(router.box.glob(".update.*"))


def test_cut_off_download_changes_nothing(router: Router) -> None:
    router.cut_release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "incomplete" in result.stdout
    assert router.unchanged()


def test_leftover_download_of_a_killed_update_is_removed(router: Router) -> None:
    (router.box / ".update.dEaD01").mkdir()
    (router.box / ".update.dEaD01/r.tgz").write_text("partial")
    router.release("0.2.0")
    result: subprocess.CompletedProcess[str] = router.update()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(router.box.glob(".update.*"))
