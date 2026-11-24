"""The server's own host checks: a pending reboot (note, then an alarm after a
week) and an NVIDIA driver that no longer matches its library (an alarm at once)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

DAY: float = 86400.0


def write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---- reboot required ---------------------------------------------------------------


def test_no_flag_file_is_nothing_but_still_checked(tmp_path: Path) -> None:
    import hostcheck

    found = hostcheck.reboot_required(tmp_path, now=1000.0)
    assert (found.alarms, found.notes, found.checked) == ([], [], ["reboot"])


def test_a_fresh_reboot_flag_is_a_note_then_an_alarm_after_a_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hostcheck

    born: float = 1_790_000_000.0
    write(
        tmp_path / "reboot-required",
        "*** System restart required ***\n",
        born + 5 * DAY,
    )
    write(
        tmp_path / "reboot-required.pkgs",
        "libc6\nlinux-image-7.0.0-45-generic\nlinux-base\nlibc6\n",
    )
    # The age is the birth time, not the mtime the package hooks keep moving.
    monkeypatch.setattr(hostcheck, "birth_time", lambda path: born)
    found = hostcheck.reboot_required(tmp_path, now=born + 6 * DAY)
    assert found.alarms == []
    assert found.notes == ["reboot required for libc6, kernel (pending 6 d)"]
    found = hostcheck.reboot_required(tmp_path, now=born + 7 * DAY)
    assert found.alarms == [
        {
            "key": "reboot/required",
            "text": "reboot required for libc6, kernel (pending 7 d)",
        }
    ]
    assert found.notes == []


def test_without_a_birth_time_the_oldest_mtime_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hostcheck

    monkeypatch.setattr(hostcheck, "birth_time", lambda path: None)
    write(tmp_path / "reboot-required", "", 1000.0 + 8 * DAY)
    write(tmp_path / "reboot-required.pkgs", "dbus\n", 1000.0)
    found = hostcheck.reboot_required(tmp_path, now=1000.0 + 8 * DAY)
    assert [a["key"] for a in found.alarms] == ["reboot/required"]


def test_birth_time_reads_this_filesystem_or_says_it_cannot(tmp_path: Path) -> None:
    import hostcheck

    path: Path = write(tmp_path / "f", "x")
    born: float | None = hostcheck.birth_time(path)
    # Not every filesystem records it; when it does, it is about now.
    assert born is None or abs(born - path.stat().st_mtime) < 5
    assert hostcheck.birth_time(tmp_path / "missing") is None


# ---- NVIDIA driver / library mismatch -----------------------------------------------


def nvidia(tmp_path: Path, module: str | None, library: str | None) -> dict[str, Any]:
    """A fake /sys, /proc and lib dir for nvidia_mismatch."""
    sys_module: Path = tmp_path / "sys" / "version"
    if module:
        write(sys_module, module + "\n")
    lib: Path = tmp_path / "lib"
    lib.mkdir()
    if library:
        target: Path = write(lib / f"libnvidia-ml.so.{library}", "")
        (lib / "libnvidia-ml.so.1").symlink_to(target.name)
    return {
        "sys_module": sys_module,
        "proc": tmp_path / "proc" / "version",
        "lib_dirs": (str(tmp_path / "nowhere"), str(lib)),
    }


def test_matching_driver_is_checked_and_quiet(tmp_path: Path) -> None:
    import hostcheck

    found = hostcheck.nvidia_mismatch(**nvidia(tmp_path, "580.178.04", "580.178.04"))
    assert (found.alarms, found.checked) == ([], ["gpu/driver-mismatch"])


def test_no_nvidia_at_all_is_no_findings(tmp_path: Path) -> None:
    import hostcheck

    for module, library in ((None, "580.178.04"), ("580.178.04", None), (None, None)):
        sub: Path = tmp_path / f"{module}-{library}"
        sub.mkdir()
        found = hostcheck.nvidia_mismatch(**nvidia(sub, module, library))
        assert (found.alarms, found.checked) == ([], [])


@pytest.mark.parametrize(
    ("on_disk", "fix"),
    [
        ("580.190.02", "a reboot fixes it"),
        ("580.178.04", "the kernel module on disk is 580.178.04: reboot into a kernel"),
        (None, "reboot to finish the driver update"),
    ],
)
def test_a_mismatch_is_an_alarm_that_says_whether_a_reboot_fixes_it(
    tmp_path: Path, on_disk: str | None, fix: str
) -> None:
    import hostcheck

    found = hostcheck.nvidia_mismatch(
        **nvidia(tmp_path, "580.178.04", "580.190.02"), ondisk=lambda: on_disk
    )
    assert [a["key"] for a in found.alarms] == ["gpu/driver-mismatch"]
    text: str = found.alarms[0]["text"]
    assert text.startswith(
        "NVIDIA driver 580.190.02 installed, kernel module 580.178.04 loaded"
    )
    assert fix in text


def test_the_proc_file_is_read_when_sys_is_not_there(tmp_path: Path) -> None:
    import hostcheck

    proc: Path = write(
        tmp_path / "proc",
        "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.95.05  Release Build\n",
    )
    assert hostcheck.loaded_nvidia_version(tmp_path / "none", proc) == "580.95.05"
    write(proc, "NVRM version: NVIDIA UNIX x86_64 Kernel Module  390.157  Wed Oct 12\n")
    assert hostcheck.loaded_nvidia_version(tmp_path / "none", proc) == "390.157"


# ---- the server takes them in --------------------------------------------------------


def test_host_findings_join_the_servers_own_alarm_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostcheck
    import server

    monkeypatch.setattr(
        server,
        "get_hw_sensors",
        lambda: {
            "alarm_items": [],
            "checked": ["fan/x"],
            "notes": ["hint"],
            "rows": [],
        },
    )
    mismatch: dict[str, Any] = {
        "key": "gpu/driver-mismatch",
        "text": "NVIDIA driver ...",
    }
    monkeypatch.setattr(
        hostcheck,
        "host_findings",
        lambda: hostcheck.Findings(
            [mismatch], ["reboot required (pending 1 d)"], ["reboot"]
        ),
    )
    found: dict[str, Any] = server.server_findings()
    assert found["alarm_items"] == [mismatch]
    assert found["alarms"] == ["NVIDIA driver ..."]
    assert found["checked"] == ["fan/x", "reboot"]
    assert found["notes"] == ["hint", "reboot required (pending 1 d)"]


def test_a_failing_nvidia_smi_is_logged_once_with_its_stdout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import server

    failed = subprocess.CompletedProcess(
        ["nvidia-smi"],
        18,
        "Failed to initialize NVML: Driver/library version mismatch\n"
        "NVML library version: 580.190\n",
        "",
    )
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: failed)
    assert server.get_gpu_stats() == [] and server.get_gpu_stats() == []
    lines: list[str] = [
        r.getMessage() for r in caplog.records if "nvidia-smi" in r.getMessage()
    ]
    assert lines == [
        "nvidia-smi failed (exit 18): Failed to initialize NVML: Driver/library version"
        " mismatch (the driver was updated under the running module: reboot)"
    ]


def test_unreadable_reboot_flag_holds_an_alarm_but_absence_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hostcheck
    import server

    flag = write(tmp_path / "reboot-required", "", 1000)
    monkeypatch.setattr(hostcheck, "birth_time", lambda path: None)
    now = 1000 + 8 * DAY
    first = hostcheck.reboot_required(tmp_path, now=now)
    known, _ = server.track_alarms({}, first.alarms, first.checked, now, 3)
    stat = Path.stat

    def denied(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == flag:
            raise PermissionError("permission denied")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", denied)
        unknown = hostcheck.reboot_required(tmp_path, now=now)
    assert unknown.checked == []
    assert "cannot read reboot requirement" in unknown.notes[0]
    for step in range(1, 4):
        known, events = server.track_alarms(
            known, unknown.alarms, unknown.checked, now + step, 3
        )
        assert events == []
    assert known["reboot/required"]["since"] == now
    flag.unlink()
    recovered = hostcheck.reboot_required(tmp_path, now=now)
    for step in range(4, 7):
        known, events = server.track_alarms(
            known, recovered.alarms, recovered.checked, now + step, 3
        )
    assert known == {}
    assert events[0]["event"] == "cleared"
