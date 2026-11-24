"""Host checks the unprivileged server makes itself: a pending reboot and an NVIDIA
driver that no longer matches its library. ZFS pools are in ``zfs.py``.

Each check returns a ``Findings``: alarms ``{key, text}`` for the server's alarm
history, notes (plain strings) and the key prefixes it really looked at
(``checked``), so an alarm clears only when its check ran and found it gone.
Every path is a parameter, so tests pass a fake tree; a box without the thing a
check looks for gets no findings at all."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final


@dataclass
class Findings:
    alarms: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def merge(self, other: Findings) -> Findings:
        return Findings(
            self.alarms + other.alarms,
            self.notes + other.notes,
            self.checked + other.checked,
        )


# ---- reboot required ---------------------------------------------------------------

# A pending reboot is a note at once and an alarm after a week.
REBOOT_ALARM_AFTER_SEC: Final[float] = 7 * 86400

_AT_FDCWD: Final[int] = -100
_STATX_BTIME: Final[int] = 0x800
_libc: Any = None


def birth_time(path: Path) -> float | None:
    """When ``path`` was created (statx STATX_BTIME), or None when the filesystem
    or the platform does not say. Python has no st_birthtime on Linux before
    os.statx, so this calls libc's statx directly."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(None, use_errno=True)
        buf: ctypes.Array[ctypes.c_char] = ctypes.create_string_buffer(256)
        if _libc.statx(_AT_FDCWD, os.fsencode(path), 0, _STATX_BTIME, buf) != 0:
            return None
        mask: int = int.from_bytes(buf.raw[0:4], sys.byteorder)
        if not mask & _STATX_BTIME:
            return None
        # struct statx: stx_btime (a statx_timestamp, tv_sec first) at offset 80.
        seconds: int = int.from_bytes(buf.raw[80:88], sys.byteorder, signed=True)
        return float(seconds) if seconds > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def reboot_required(run: Path = Path("/run"), now: float | None = None) -> Findings:
    """Debian/Ubuntu's /run/reboot-required: a note while it is under a week old,
    then an alarm. Its age is the file's birth time: the package hooks rewrite it
    in place (``>`` and ``touch``) for every package, so mtime is the newest
    package, not the first. /run is tmpfs, so a reboot removes it and the alarm
    clears. Elsewhere (no update-notifier) there is nothing to read: no findings."""
    flag: Path = run / "reboot-required"
    pkgs_file: Path = run / "reboot-required.pkgs"
    found: Findings = Findings(checked=["reboot"])
    try:
        st: os.stat_result = flag.stat()
    except FileNotFoundError:
        return found
    except OSError as e:
        return Findings(notes=[f"cannot read reboot requirement: {e}"])
    now = time.time() if now is None else now
    since: float = birth_time(flag) or min([st.st_mtime] + _mtimes(pkgs_file))
    pkgs: list[str] = []
    try:
        line: str
        for line in pkgs_file.read_text().splitlines():
            name: str = "kernel" if line.strip().startswith("linux-") else line.strip()
            if name and name not in pkgs:
                pkgs.append(name)
    except OSError:
        pass
    days: float = max(0.0, now - since) / 86400
    what: str = f" for {', '.join(pkgs)}" if pkgs else ""
    text: str = f"reboot required{what} (pending {days:.0f} d)"
    if now - since >= REBOOT_ALARM_AFTER_SEC:
        found.alarms.append({"key": "reboot/required", "text": text})
    else:
        found.notes.append(text)
    return found


def _mtimes(path: Path) -> list[float]:
    try:
        return [path.stat().st_mtime]
    except OSError:
        return []


# ---- NVIDIA driver / library mismatch -----------------------------------------------

_MODULE_VERSION: Final[re.Pattern[str]] = re.compile(
    r"Kernel Module(?: for \S+)?\s+(\d+(?:\.\d+)+)"
)
_LIB_VERSION: Final[re.Pattern[str]] = re.compile(r"libnvidia-ml\.so\.(\d+(?:\.\d+)+)$")
NVIDIA_LIB_DIRS: Final[tuple[str, ...]] = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib64",
    "/usr/lib",
    "/usr/lib/nvidia",
    "/usr/lib/x86_64-linux-gnu/nvidia/current",
)
MISMATCH_TEXT: Final[str] = "Driver/library version mismatch"


def loaded_nvidia_version(
    sys_module: Path = Path("/sys/module/nvidia/version"),
    proc: Path = Path("/proc/driver/nvidia/version"),
) -> str | None:
    """The NVIDIA kernel module running now, or None when none is loaded."""
    try:
        return sys_module.read_text().strip() or None
    except OSError:
        pass
    try:
        match: re.Match[str] | None = _MODULE_VERSION.search(proc.read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def installed_nvml_version(lib_dirs: tuple[str, ...] = NVIDIA_LIB_DIRS) -> str | None:
    """The version of the installed libnvidia-ml (the userspace driver), from the
    file libnvidia-ml.so.1 resolves to, or None when there is none."""
    d: str
    for d in lib_dirs:
        link: Path = Path(d) / "libnvidia-ml.so.1"
        try:
            target: str = os.path.realpath(link, strict=True)
        except OSError:
            continue
        match: re.Match[str] | None = _LIB_VERSION.search(target)
        if match:
            return match.group(1)
    return None


def _ondisk_module_version() -> str | None:
    """The nvidia module installed for the running kernel (modinfo), or None."""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["modinfo", "-F", "version", "nvidia"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def nvidia_mismatch(
    sys_module: Path = Path("/sys/module/nvidia/version"),
    proc: Path = Path("/proc/driver/nvidia/version"),
    lib_dirs: tuple[str, ...] = NVIDIA_LIB_DIRS,
    ondisk: Any = _ondisk_module_version,
) -> Findings:
    """An alarm at once when the loaded NVIDIA kernel module and the installed
    library differ: nvidia-smi and NVML then fail ("Driver/library version
    mismatch") and every GPU reading is gone until a reboot. Read from /sys and
    the library's file name, not nvidia-smi, so it works exactly when nvidia-smi
    does not. A userspace-only update causes it without /run/reboot-required.

    When the module on disk for the running kernel is still the old one, a
    reboot into the same kernel does not fix it: the text says so."""
    loaded: str | None = loaded_nvidia_version(sys_module, proc)
    library: str | None = installed_nvml_version(lib_dirs)
    if loaded is None or library is None:
        return Findings()
    found: Findings = Findings(checked=["gpu/driver-mismatch"])
    if loaded == library:
        return found
    on_disk: str | None = ondisk()
    fix: str = (
        "a reboot fixes it"
        if on_disk == library
        else f"the kernel module on disk is {on_disk}: reboot into a kernel with a {library} module"
        if on_disk
        else "reboot to finish the driver update"
    )
    found.alarms.append(
        {
            "key": "gpu/driver-mismatch",
            "text": f"NVIDIA driver {library} installed, kernel module {loaded} loaded:"
            f" GPU readings are offline; {fix}",
        }
    )
    return found


def host_findings() -> Findings:
    """Every host check, for the server's own alarm source."""
    return reboot_required().merge(nvidia_mismatch())
