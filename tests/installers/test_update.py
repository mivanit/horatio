"""`install.sh --update` against throwaway git repositories.

Each test builds an upstream repo whose commits are releases. The release a test
updates *to* carries a stub installer that records its arguments, standing in
for "the updated installer ran"; the one it updates *from* carries the real
install.sh. No network, sudo or services are involved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.support import ROOT

STUB: str = '#!/bin/bash\nprintf "%s\\n" "$*" > "$(dirname "$0")/ran"\n'


class Repos:
    def __init__(self: Repos, tmp_path: Path) -> None:
        self.upstream: Path = tmp_path / "upstream"
        self.box: Path = tmp_path / "box"
        self.env: dict[str, str] = dict(
            os.environ,
            HOME=str(tmp_path),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        self.git(self.upstream.parent, "init", "--quiet", "-b", "main", "upstream")

    def git(self: Repos, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self: Repos, name: str, *, stub: bool = False, tag: bool = True) -> None:
        """Commit upstream (tagged v<name> unless tag=False)."""
        installer: Path = self.upstream / "install.sh"
        installer.write_text(STUB if stub else (ROOT / "install.sh").read_text())
        installer.chmod(0o755)
        (self.upstream / "VERSION").write_text(name + "\n")
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "--quiet", "-m", name)
        if tag:
            self.git(self.upstream, "tag", "-a", f"v{name}", "-m", name)

    def clone(self: Repos, ref: str = "main") -> None:
        self.git(self.upstream.parent, "clone", "--quiet", str(self.upstream), "box")
        if ref != "main":
            self.git(self.box, "checkout", "--quiet", "--detach", ref)

    def update(self: Repos, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.box / "install.sh"), "--update", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def head(self: Repos) -> str:
        return self.git(self.box, "describe", "--tags", "--always")

    def ran(self: Repos) -> str | None:
        marker: Path = self.box / "ran"
        return marker.read_text().strip() if marker.exists() else None


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_newest_stable_release_runs_its_installer_with_the_other_flags(
    repos: Repos,
) -> None:
    repos.commit("0.1.0")
    repos.commit("0.2.0", stub=True)
    repos.commit("0.3.0-rc1", stub=True)
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update(
        "--no-optional", "--collector"
    )
    assert result.returncode == 0, result.stderr
    assert repos.head() == "v0.2.0", "pre-release tags are skipped unless named"
    assert repos.ran() == "--no-optional --collector"
    assert "To go back: ./install.sh --update v0.1.0" in result.stdout


def test_local_changes_are_refused(repos: Repos) -> None:
    repos.commit("0.1.0")
    repos.commit("0.2.0", stub=True)
    repos.clone("v0.1.0")
    (repos.box / "VERSION").write_text("local edit\n")
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "local changes" in result.stderr
    assert repos.head() == "v0.1.0" and repos.ran() is None


def test_a_checkout_ahead_of_the_newest_release_moves_only_when_named(
    repos: Repos,
) -> None:
    repos.commit("0.1.0", stub=True)
    repos.commit("unreleased", tag=False)
    repos.clone()
    ahead: str = repos.head()
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "not behind v0.1.0" in result.stderr
    assert repos.head() == ahead and repos.ran() is None
    result = repos.update("v0.1.0")
    assert result.returncode == 0, result.stderr
    assert repos.head() == "v0.1.0" and repos.ran() == ""
    # git describe output is itself a ref, so the hint works as printed.
    assert f"To go back: ./install.sh --update {ahead}" in result.stdout


def test_no_releases_yet(repos: Repos) -> None:
    repos.commit("0.0.0", tag=False)
    repos.clone()
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "no releases yet" in result.stderr


def test_a_failed_fetch_says_so(repos: Repos) -> None:
    repos.commit("0.1.0")
    repos.clone()
    repos.upstream.rename(repos.upstream.with_name("gone"))
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "git fetch failed" in result.stderr


def test_a_release_tag_recreated_upstream_still_updates(repos: Repos) -> None:
    repos.commit("0.1.0")
    repos.commit("0.2.0", stub=True)
    repos.clone("v0.1.0")
    repos.git(repos.upstream, "commit", "--quiet", "--allow-empty", "-m", "again")
    repos.git(repos.upstream, "tag", "-f", "-a", "v0.2.0", "-m", "0.2.0")
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 0, result.stderr
    assert repos.git(repos.box, "rev-parse", "HEAD") == repos.git(
        repos.upstream, "rev-parse", "v0.2.0^{commit}"
    )
