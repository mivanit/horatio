"""`install.sh --update` against throwaway git repositories.

Each test builds an upstream repo whose commits are releases. A release either
carries the real install.sh (the one doing the update) or a stub that records
its arguments, standing in for "the updated installer ran". No network, sudo
or services are involved: the update itself exits or execs before any of that.
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

    def release(
        self: Repos,
        version: str,
        *,
        stub: bool = False,
        tag: bool = True,
        changelog: str = "",
    ) -> None:
        """Commit `version` upstream (tagged v<version> unless tag=False)."""
        installer: Path = self.upstream / "install.sh"
        installer.write_text(STUB if stub else (ROOT / "install.sh").read_text())
        installer.chmod(0o755)
        (self.upstream / "VERSION").write_text(version + "\n")
        (self.upstream / "CHANGELOG.md").write_text(changelog or f"## [{version}]\n")
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "--quiet", "-m", version)
        if tag:
            self.git(self.upstream, "tag", "-a", f"v{version}", "-m", version)

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
    repos.release("0.1.0")
    repos.release("0.2.0", stub=True)
    repos.release("0.3.0-rc1", stub=True)
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update(
        "--no-optional", "--collector"
    )
    assert result.returncode == 0, result.stderr
    assert repos.head() == "v0.2.0", "pre-release tags are skipped unless named"
    assert repos.ran() == "--no-optional --collector"
    assert "v0.1.0 (v0.1.0) -> v0.2.0 (v0.2.0)" in result.stdout
    assert "To go back: ./install.sh --update v0.1.0" in result.stdout
    # The checkout has moved before the new installer runs: say how to finish.
    assert "rerun: ./install.sh --no-optional --collector" in result.stdout


def test_named_pre_release_and_bare_version(repos: Repos) -> None:
    repos.release("0.1.0")
    repos.release("0.3.0-rc1", stub=True)
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update("0.3.0-rc1")
    assert result.returncode == 0, result.stderr
    assert repos.head() == "v0.3.0-rc1" and repos.ran() == ""


def test_already_at_newest_release(repos: Repos) -> None:
    repos.release("0.1.0")
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 0, result.stderr
    assert "Already at v0.1.0" in result.stdout and repos.ran() is None


@pytest.mark.parametrize("ref", ["", "main"])
def test_already_current_still_runs_the_other_flags(
    repos: Repos, tmp_path: Path, ref: str
) -> None:
    """`--update --collector` on a current box installs the collector instead of
    stopping at "Already at". The installer it hands over to is the box's own
    (real) one, so a `bash` first on PATH passes the --update run through and
    records the hand-over instead of running it."""
    repos.release("0.1.0")
    repos.clone()
    if ref == "main":
        # origin/main must be fetched for "Already on the newest main".
        repos.git(repos.box, "fetch", "--quiet", "origin")
    shim: Path = tmp_path / "bin" / "bash"
    shim.parent.mkdir()
    real_bash: str = subprocess.run(
        ["sh", "-c", "command -v bash"], capture_output=True, text=True, check=True
    ).stdout.strip()
    shim.write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" --update "*) exec {real_bash} "$@" ;; esac\n'
        f'printf "%s\\n" "$*" > {repos.box}/ran\n'
    )
    shim.chmod(0o755)
    repos.env["PATH"] = f"{shim.parent}{os.pathsep}{repos.env['PATH']}"
    result: subprocess.CompletedProcess[str] = repos.update(
        *([ref] if ref else []), "--collector", "--no-optional"
    )
    assert result.returncode == 0, result.stderr
    assert "Already" in result.stdout
    assert "Running the installer for: --collector --no-optional" in result.stdout
    assert repos.ran() == f"{repos.box}/install.sh --collector --no-optional"


@pytest.mark.parametrize("problem", ["dirty", "branch", "unpushed"])
def test_refuses_to_move_local_work(repos: Repos, problem: str) -> None:
    repos.release("0.1.0")
    repos.release("0.2.0", stub=True)
    repos.clone("v0.1.0" if problem == "dirty" else "main")
    if problem == "dirty":
        (repos.box / "CHANGELOG.md").write_text("local edit\n")
        expect: str = "local changes"
    elif problem == "branch":
        repos.git(repos.box, "checkout", "--quiet", "-b", "feature", "v0.1.0")
        expect = "on branch 'feature'"
    else:
        repos.git(repos.box, "checkout", "--quiet", "-B", "main", "v0.1.0")
        (repos.box / "notes").write_text("x\n")
        repos.git(repos.box, "add", "notes")
        repos.git(repos.box, "commit", "--quiet", "-m", "local")
        expect = "not on origin/main"
    before: str = repos.git(repos.box, "rev-parse", "HEAD")
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and expect in result.stderr, result.stderr
    assert repos.git(repos.box, "rev-parse", "HEAD") == before
    assert repos.ran() is None


def test_main_between_releases_needs_an_explicit_choice(repos: Repos) -> None:
    repos.release("0.1.0", stub=True)
    repos.release("0.1.0", tag=False)  # main moves on; VERSION not bumped yet
    repos.clone()
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "unreleased commits" in result.stderr
    assert repos.ran() is None
    result = repos.update("v0.1.0")
    assert result.returncode == 0, result.stderr
    assert repos.head() == "v0.1.0" and repos.ran() == ""
    assert "To go back: git checkout main && ./install.sh" in result.stdout


def test_downgrade_only_when_named(repos: Repos) -> None:
    repos.release("0.2.0", stub=True)
    repos.release("0.3.0", tag=False)
    repos.clone()
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 1 and "--update v0.2.0" in result.stderr
    result = repos.update("v0.2.0")
    assert result.returncode == 0, result.stderr
    assert "downgrading v0.3.0 -> v0.2.0" in result.stderr
    assert repos.head() == "v0.2.0" and repos.ran() == ""


def test_main_channel_fast_forwards_onto_the_branch(repos: Repos) -> None:
    repos.release("0.1.0")
    repos.release("0.2.0", stub=True, tag=False)
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update("main")
    assert result.returncode == 0, result.stderr
    assert repos.git(repos.box, "symbolic-ref", "--short", "HEAD") == "main"
    assert repos.git(repos.box, "rev-parse", "HEAD") == repos.git(
        repos.upstream, "rev-parse", "HEAD"
    )
    assert repos.ran() == ""


def test_major_update_prints_the_changes_in_between(repos: Repos) -> None:
    repos.release("0.2.0")
    repos.release(
        "1.0.0",
        stub=True,
        changelog="# Changelog\n\n## [Unreleased]\n\n- pending\n\n"
        "## [1.0.0]\n\n- renamed a config key\n\n## [0.2.0]\n\n- older\n",
    )
    repos.clone("v0.2.0")
    result: subprocess.CompletedProcess[str] = repos.update()
    assert result.returncode == 0, result.stderr
    assert "Major update v0.2.0 -> v1.0.0" in result.stdout
    assert "renamed a config key" in result.stdout
    assert "pending" not in result.stdout and "older" not in result.stdout


def test_unknown_release_and_uninstall_conflict(repos: Repos) -> None:
    repos.release("0.1.0")
    repos.clone("v0.1.0")
    result: subprocess.CompletedProcess[str] = repos.update("v9.9.9")
    assert result.returncode == 1 and "no release v9.9.9" in result.stderr
    result = repos.update("--uninstall")
    assert result.returncode == 2 and "do not go together" in result.stderr
