"""scripts/history-merge.sh: merging history from routers and Linux boxes, under
dash with the system's tools and under BusyBox with only its applets (a router)."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.support import ROOT

SCRIPT: Path = ROOT / "scripts" / "history-merge.sh"
Run = Callable[..., subprocess.CompletedProcess[str]]
APPLETS: tuple[str, ...] = ("awk", "sort", "gzip", "cat", "mv", "rm", "touch")


def router(ts: str, v: int = 1) -> str:
    """A record as the router writes it: timestamp not first, no fraction."""
    return f'{{"cpu": {{"total": {v}}}, "timestamp": "{ts}+00:00"}}\n'


def linux(ts: str, v: int = 1) -> str:
    """A record as server.py writes it: timestamp first, microseconds."""
    return f'{{"timestamp": "{ts}.250000+00:00", "cpu": {{"avg": {v}}}}}\n'


@pytest.fixture(params=["dash", "busybox"])
def run(request: pytest.FixtureRequest, tmp_path: Path) -> Run:
    """Run the script with this shell; BusyBox gets a PATH of its applets only."""
    env: dict[str, str] = dict(os.environ)
    if request.param == "busybox":
        busybox: str | None = shutil.which("busybox")
        if busybox is None:
            pytest.skip("busybox not installed")
        bin_dir: Path = tmp_path / "bb"
        bin_dir.mkdir()
        applet: str
        for applet in APPLETS:
            (bin_dir / applet).symlink_to(busybox)
        env["PATH"] = str(bin_dir)
        shell: list[str] = [busybox, "sh"]
    else:
        if shutil.which("dash") is None:
            pytest.skip("dash not installed")
        shell = ["dash"]

    def go(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*shell, str(SCRIPT), *args],
            env={**env, "PATH": path} if path else env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return go


def digest(directory: Path) -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def test_merge(run: Run, tmp_path: Path) -> None:
    logs: Path = tmp_path / "router-logs"
    logs.mkdir()
    # An archive, then the live file, overlapping by one identical record.
    (logs / "history-20260101T000000Z.jsonl.gz").write_bytes(
        gzip.compress(
            (router("2026-01-01T00:00:00") + router("2026-01-01T00:10:00")).encode()
        )
    )
    (logs / "history.jsonl").write_text(
        router("2026-01-01T00:10:00")  # exact duplicate of the archive's last
        + router("2026-01-01T00:20:00", v=1)
        + router("2026-01-01T00:20:00", v=2)  # same time, different record: kept
        + '{"timestamp": "2026-01-01T00:3'  # torn last line, no newline
    )
    other: Path = tmp_path / "box.jsonl"
    other.write_text(
        linux("2026-01-01T00:05:00")
        + "\n"
        + linux("2026-01-01T00:20:00")  # after the router's 00:20:00 records
        + router("2026-01-01T00:00:00")  # identical to a router line: dropped
    )
    before: dict[str, str] = digest(tmp_path)
    out: Path = tmp_path / "merged.jsonl"
    result: subprocess.CompletedProcess[str] = run(
        "-o", str(out), str(logs), str(other)
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == [
        '{"timestamp": "2026-01-01T00:3',  # no complete timestamp: kept, first
        router("2026-01-01T00:00:00").strip(),
        linux("2026-01-01T00:05:00").strip(),
        router("2026-01-01T00:10:00").strip(),
        router("2026-01-01T00:20:00", v=1).strip(),
        router("2026-01-01T00:20:00", v=2).strip(),
        linux("2026-01-01T00:20:00").strip(),
    ]
    assert "7 records from 9 lines; 2 exact duplicates dropped" in result.stderr
    assert "1 lines without a timestamp kept" in result.stderr
    after: dict[str, str] = digest(tmp_path)
    assert all(after.get(k) == v for k, v in before.items()), "inputs unchanged"
    assert sorted(p.name for p in tmp_path.iterdir() if "merged" in p.name) == [
        "merged.jsonl"
    ], "no temporary files left"


def test_refuses_existing_output(run: Run, tmp_path: Path) -> None:
    src: Path = tmp_path / "a.jsonl"
    src.write_text(router("2026-01-01T00:00:00"))
    out: Path = tmp_path / "out.jsonl"
    out.write_text("keep me")
    result: subprocess.CompletedProcess[str] = run("-o", str(out), str(src))
    assert result.returncode == 1 and "already exists" in result.stderr
    assert out.read_text() == "keep me"


def test_damaged_archive_uses_what_reads(run: Run, tmp_path: Path) -> None:
    """A killed rotation's partial archive: its readable records are used; the
    rest are in the live file it was being cut from."""
    data: bytes = gzip.compress(
        "".join(router(f"2026-01-01T{h:02}:00:00") for h in range(20)).encode()
    )
    damaged: Path = tmp_path / "history-20260101T000000Z.jsonl.gz"
    damaged.write_bytes(data[: len(data) // 2])
    live: Path = tmp_path / "history.jsonl"
    live.write_text("".join(router(f"2026-01-01T{h:02}:00:00") for h in range(20)))
    out: Path = tmp_path / "out.jsonl"
    result: subprocess.CompletedProcess[str] = run("-o", str(out), str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "damaged" in result.stderr
    lines: list[str] = out.read_text().splitlines()
    assert all(r.strip() in lines for r in live.read_text().splitlines(True))


def test_failed_sort_writes_nothing(
    run: Run, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if "busybox" in request.node.name:
        pytest.skip("BusyBox sh runs its own sort applet, not one on PATH")
    src: Path = tmp_path / "a.jsonl"
    src.write_text(router("2026-01-01T00:00:00"))
    fake: Path = tmp_path / "fake"
    fake.mkdir()
    (fake / "sort").write_text("#!/bin/sh\ncat > /dev/null\nexit 1\n")
    (fake / "sort").chmod(0o755)
    out: Path = tmp_path / "out.jsonl"
    result: subprocess.CompletedProcess[str] = run(
        "-o",
        str(out),
        str(src),
        path=f"{fake}:{os.environ['PATH']}",
    )
    assert result.returncode == 1 and "sort failed" in result.stderr
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("out")] == []
