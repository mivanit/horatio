# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "glances[web]",
#     "nvidia-ml-py",
#     "httpx",
# ]
# ///
"""
Horatio's server: serves the static frontend, spawns Glances as a subprocess and
proxies /api/4/* to it, adds its own endpoints (GPU, ping, IP info, per-process
network and disk, local mail, the collector's snapshots) and records history.

Run: uv run python server.py   (config.json must exist; ./install.sh creates it)
"""

from __future__ import annotations

import argparse
import asyncio
import email
import email.policy
import getpass
import hashlib
import hmac
import json
import logging
import mailbox
import math
import os
import posixpath
import re
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final, NamedTuple

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
INDEX_FILE: Final[Path] = Path(__file__).parent / "index.html"
LOGS_DIR: Final[Path] = Path(__file__).parent / "logs"
HISTORY_FILE: Final[Path] = LOGS_DIR / "history.jsonl"
# ~1 MiB covers ~3 days of records at the default 600s interval (~2.3 KB/record)
HISTORY_INITIAL_TAIL_READ: Final[int] = 1024 * 1024
# Hard cap on how much of the tail we'll read for one request, so a huge history
# file (no rotation) can never make a read unbounded. A range that reaches this
# cap before covering its cutoff returns truncated=True (see _read_history). 64 MiB
# comfortably covers the UI's 30-day max even at a dense recording cadence.
HISTORY_MAX_TAIL_READ: Final[int] = 64 * 1024 * 1024
# Record fields /api/history leaves out because they dwarf the rest (a 72-core
# box writes ~70 temps per record; the process rows are several KB): a card
# that draws one asks for it alone via /api/history/field/<name>.
HISTORY_SIDE_FIELDS: Final[frozenset[str]] = frozenset({"processes", "temps"})
PASSWORD_FILE: Final[Path] = Path(__file__).parent / ".password"
CONFIG_FILE: Final[Path] = Path(__file__).parent / "config.json"
DEFAULT_PORT: Final[int] = 8080
DEFAULT_GLANCES_PORT: Final[int] = 61208
# All interfaces by default (the dashboard is meant to be reached from other
# devices); override with --host 127.0.0.1 to keep it local-only.
DEFAULT_HOST: Final[str] = "0.0.0.0"


def _load_config() -> dict[str, Any]:
    """Load config.json, the per-machine config install.sh creates from
    config.example.json. Missing or malformed is fatal on purpose: silently
    running on defaults is how a box ends up pinging the wrong router for
    months."""
    if not CONFIG_FILE.exists():
        sys.exit(
            f"{CONFIG_FILE} is missing: run ./install.sh, or "
            f"cp {CONFIG_FILE.parent / 'config.example.json'} {CONFIG_FILE}"
        )
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{CONFIG_FILE}: invalid JSON: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"{CONFIG_FILE}: must be a JSON object, got {type(cfg).__name__}")
    return cfg


def _cfg_int(
    cfg: dict[str, Any],
    key: str,
    default: int,
    lo: int,
    hi: int,
    section: str = "history",
) -> int:
    """Read an int setting from one config.json section, clamped to [lo, hi].

    For ``history`` the bounds are the same ones /api/history/settings enforces.
    Unvalidated, an out-of-range config value produces a silently degenerate
    recorder: ``"sample_count": 0`` makes ``range(0)`` take no samples at all, so
    every ping target is recorded as a full outage that never actually happened.
    """
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        logging.getLogger("dashboard").warning(
            "config.json %s.%s=%r is invalid or outside [%d, %d]; using %d",
            section,
            key,
            value,
            lo,
            hi,
            default,
        )
        return default
    return value


_CONFIG: Final[dict[str, Any]] = _load_config()

# History recording settings (defaults, runtime values stored on APP.state).
# Bounds mirror the validation in the settings API so both paths agree.
_history_cfg: dict[str, Any] = _CONFIG.get("history", {})
if not isinstance(_history_cfg, dict):
    # `"history": null` or a scalar would otherwise raise in _cfg_int at import.
    logging.getLogger("dashboard").warning(
        "config.json: 'history' is not an object; using defaults"
    )
    _history_cfg = {}
DEFAULT_HISTORY_RECORD_INTERVAL: Final[int] = _cfg_int(
    _history_cfg, "record_interval", 600, 10, 86400
)
DEFAULT_HISTORY_SAMPLE_DURATION: Final[int] = _cfg_int(
    _history_cfg, "sample_duration", 10, 1, 300
)
DEFAULT_HISTORY_SAMPLE_COUNT: Final[int] = _cfg_int(
    _history_cfg, "sample_count", 5, 1, 100
)
# Per-process history: off unless config.json (or the settings popup) turns it
# on; each record then carries the top `process_count` processes by CPU and by
# RSS plus every process holding GPU memory (see build_process_records).
DEFAULT_HISTORY_PROCESS_COUNT: Final[int] = _cfg_int(
    _history_cfg, "process_count", 10, 1, 50
)
_processes_enabled: Any = _history_cfg.get("processes_enabled", False)
if not isinstance(_processes_enabled, bool):
    logging.getLogger("dashboard").warning(
        "config.json history.processes_enabled=%r is not a boolean; using false",
        _processes_enabled,
    )
    _processes_enabled = False
DEFAULT_HISTORY_PROCESSES_ENABLED: Final[bool] = _processes_enabled


# Password hashing
PBKDF2_ITERATIONS: Final[int] = 600_000

# Request headers never forwarded to Glances. "host" must be rewritten; the
# credential headers are dropped so a browser's cookies/auth for this origin are
# not handed to the upstream process.
PROXY_STRIPPED_REQUEST_HEADERS: Final[set[str]] = {
    "host",
    "cookie",
    "authorization",
    "proxy-authorization",
}

# Headers that should not be forwarded when proxying
HOP_BY_HOP_HEADERS: Final[set[str]] = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "content-encoding",
}

logger: logging.Logger = logging.getLogger("dashboard")

# Client connection tracking
CLIENT_TIMEOUT: Final[int] = 30  # seconds
client_last_seen: dict[str, float] = {}  # IP -> timestamp

# Lock to prevent concurrent nethogs captures
_nethogs_lock: asyncio.Lock = asyncio.Lock()

# Lock to prevent concurrent disk captures
_disk_lock: asyncio.Lock = asyncio.Lock()

# Rate limiting for manual history recording
# Minimum gap between manual "record now" requests; 0 disables the limit. Read
# from config.json like the other history settings so it survives restarts.
DEFAULT_MANUAL_RECORD_MIN_INTERVAL: Final[int] = _cfg_int(
    _history_cfg, "min_record_interval", 60, 0, 3600
)
_last_manual_record_time: float = 0.0


def _hash_password(password: str) -> str:
    """Generate salt:hash string for a password using PBKDF2-HMAC-SHA256."""
    salt = secrets.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{salt.hex()}:{hash_bytes.hex()}"


def _verify_password(password: str) -> bool:
    """Verify password against stored PBKDF2 hash. Returns False if no password file."""
    if not PASSWORD_FILE.exists():
        return False
    try:
        stored = PASSWORD_FILE.read_text().strip()
        salt_hex, hash_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except (ValueError, FileNotFoundError):
        return False

    computed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(computed, expected_hash)


# Throttle the password-verify endpoints. The PBKDF2 check is deliberately
# expensive (PBKDF2_ITERATIONS), so an unauthenticated client must not be able to
# spam it (CPU exhaustion) or brute-force freely. Track consecutive failures per
# client IP and lock that IP out for a spell once it crosses the threshold.
AUTH_MAX_FAILS: Final[int] = 5
AUTH_LOCKOUT_SEC: Final[int] = 60
_auth_failures: dict[str, tuple[int, float]] = {}  # ip -> (consecutive_fails, locked_until)


async def _verify_password_throttled(request: Request, password: str) -> Response | None:
    """Verify ``password`` rate-limited per client IP.

    Returns an error Response to send back — 429 while the IP is locked out, 403 on
    a wrong password — or None when the password is correct. A correct password
    clears the IP's failure streak; the lockout is what makes the costly KDF
    unspammable.
    """
    ip: str = request.client.host if request.client else "unknown"
    now: float = time.time()
    fails, until = _auth_failures.get(ip, (0, 0.0))
    if now < until:
        return Response(
            content=json.dumps(
                {"error": "Too many attempts", "retry_after": int(until - now)}
            ),
            status_code=429,
            media_type="application/json",
        )
    if until:
        fails = 0  # a served lockout resets the streak so a returning user starts fresh
    if not await asyncio.to_thread(_verify_password, password):
        fails += 1
        locked = now + AUTH_LOCKOUT_SEC if fails >= AUTH_MAX_FAILS else 0.0
        _auth_failures[ip] = (fails, locked)
        return Response(
            content=json.dumps({"error": "Incorrect password"}),
            status_code=403,
            media_type="application/json",
        )
    _auth_failures.pop(ip, None)
    return None


def get_sanitized_process_info(
    pid: int, fallback_name: str = "unknown", fallback_path: str = ""
) -> dict[str, str]:
    """
    Get sanitized process info from /proc/<pid>/cmdline.
    Returns {"name": "...", "path": "..."} with sensitive args stripped.

    Rules:
    - Always include executable path
    - For python -m module: include module name
    - For interpreter + script: include script path
    - Never include other arguments (may contain passwords, tokens, etc.)
    """
    # PID 0 is the kernel scheduler - has no cmdline
    if pid == 0:
        return {"name": "kernel", "path": ""}

    try:
        cmdline_raw = Path(f"/proc/{pid}/cmdline").read_text()
        cmdline: list[str] = [c for c in cmdline_raw.split("\0") if c]

        # Fallback: if only 1 element with spaces, it's space-separated (e.g., Firefox snap)
        if len(cmdline) == 1 and " " in cmdline[0]:
            cmdline = cmdline[0].split()

        if not cmdline:
            logger.debug("PID %d: cmdline empty", pid)
            return {"name": fallback_name, "path": fallback_path}

        exe = cmdline[0]
        exe_name = Path(exe).name
        args: list[str] = cmdline[1:]

        # Python with -m module
        if "python" in exe_name.lower():
            for i, arg in enumerate(args):
                if arg == "-m" and i + 1 < len(args):
                    module = args[i + 1]
                    return {"name": module, "path": f"{exe} -m {module}"}
            # Python with script file (first non-flag argument)
            for arg in args:
                if not arg.startswith("-"):
                    return {"name": Path(arg).name, "path": f"{exe} {arg}"}

        # Other interpreters (node, ruby, etc.) with script
        interpreters: set[str] = {"node", "ruby", "perl", "php"}
        if exe_name in interpreters or any(
            exe_name.startswith(f"{i}.") for i in interpreters
        ):
            for arg in args:
                if not arg.startswith("-"):
                    return {"name": Path(arg).name, "path": f"{exe} {arg}"}

        # Regular executable - just show the executable
        return {"name": exe_name, "path": exe}

    except (OSError, PermissionError, FileNotFoundError, IndexError, ValueError) as e:
        # ValueError covers UnicodeDecodeError: an argv with non-UTF-8 bytes is
        # one process's problem, not the whole process list's.
        logger.debug("PID %d cmdline read failed: %s", pid, e)
        return {"name": fallback_name, "path": fallback_path}


def start_glances(port: int) -> subprocess.Popen[bytes]:
    """Start the Glances web server subprocess."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "glances",
            "--webserver",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--quiet",
            # Glances otherwise phones PyPI on every start and tries to create
            # ~/.cache/glances, which is read-only under the systemd unit.
            "--disable-check-update",
        ],
        stdout=subprocess.DEVNULL,
        # Its crash reason, if it ever dies (the watchdog in
        # check_disconnected_clients restarts it and points here).
        stderr=(LOGS_DIR / "glances.log").open("ab"),
    )


def stop_glances(process: subprocess.Popen[bytes]) -> None:
    """Stop the Glances subprocess."""
    if process.poll() is None:
        logger.info("Stopping Glances process...")
        process.terminate()  # SIGTERM first (graceful)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Glances didn't stop gracefully, killing...")
            process.kill()
            process.wait()
        logger.info("Glances process stopped")


# ============================================================================
# GPU Memory Helper
# ============================================================================


def _parse_gpu_stats(stdout: str) -> list[dict[str, Any]]:
    """Parse nvidia-smi --query-gpu CSV (index,util,mem.used,mem.total,temp,power,
    mem temp, sw/hw thermal slowdown counters).

    Each metric field degrades to None on '[N/A]'/'[Not Supported]' (nvidia-smi's
    tokens for unreadable fields) rather than dropping the whole GPU row; only an
    unparseable index drops the row, since the index is the GPU's key. Pure (no
    I/O) so the parsing — including the N/A handling — is unit-testable.
    """

    def _f(s: str) -> float | None:
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    gpus: list[dict[str, Any]] = []
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            logger.warning("Skipping malformed nvidia-smi GPU line: %s", line)
            continue
        try:
            index = int(parts[0])
        except (ValueError, TypeError):
            logger.warning("Skipping nvidia-smi GPU line with bad index: %s", line)
            continue
        gpus.append(
            {
                "index": index,
                "util_percent": _f(parts[1]),
                "memory_used_mb": _f(parts[2]),
                "memory_total_mb": _f(parts[3]),
                "temperature": _f(parts[4]),
                # The history power record reads this; an older 5-column line is
                # tolerated so the parser's unit tests stay valid.
                "power_draw": _f(parts[5]) if len(parts) > 5 else None,
                # Memory (HBM) temperature: N/A on GDDR cards (the P40). The
                # counters are microseconds of thermal slowdown since driver load.
                # Absent on an older 5/6-column line, like power_draw.
                "temperature_memory": _f(parts[6]) if len(parts) > 6 else None,
                "throttle_sw_thermal_us": _f(parts[7]) if len(parts) > 7 else None,
                "throttle_hw_thermal_us": _f(parts[8]) if len(parts) > 8 else None,
            }
        )
    return gpus


def get_gpu_stats() -> list[dict[str, Any]]:
    """GPU utilization, memory, and temperature from nvidia-smi (driver-native).

    History sources GPU metrics from nvidia-smi rather than Glances because
    Glances' pynvml binding returns all-null for some datacenter GPUs (e.g. the
    Tesla P40) while the driver's own CLI reads them fine. Per-field
    None-preserving; empty list on failure (nvidia-smi missing/timeout/error).
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,"
                "temperature.memory,clocks_event_reasons_counters.sw_thermal_slowdown,"
                "clocks_event_reasons_counters.hw_thermal_slowdown",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning("nvidia-smi failed: %s", result.stderr.strip())
            return []
        return _parse_gpu_stats(result.stdout)
    except FileNotFoundError:
        logger.debug("nvidia-smi not found")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi timed out")
        return []
    except Exception as e:
        logger.warning("nvidia-smi error: %s", e)
        return []


# ============================================================================
# Ping Helper
# ============================================================================

PING_TARGETS: Final[list[str]] = [
    addr
    for t in _CONFIG.get(
        "ping_targets",
        [{"address": "8.8.8.8"}, {"address": "192.168.1.1"}],
    )
    if (addr := t.get("address"))
]


def _run_ping(target: str = "8.8.8.8", timeout: int = 2) -> float | None:
    """Run a single ping and return latency in ms, or None on failure."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        if result.returncode != 0:
            return None
        # Parse "time=12.3 ms" from ping output
        for line in result.stdout.split("\n"):
            if "time=" in line:
                for part in line.split():
                    if part.startswith("time="):
                        return float(part.split("=")[1])
        return None
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return None


# ============================================================================
# History Recording
# ============================================================================


def _parse_gpu_index(gpu: dict) -> int:
    """Extract numeric GPU index from Glances gpu_id (e.g. 'nvidia0' -> 0)."""
    glances_id = gpu.get("gpu_id", 0)
    if isinstance(glances_id, str) and glances_id.startswith("nvidia"):
        try:
            return int(glances_id.replace("nvidia", ""))
        except ValueError:
            return 0
    return glances_id if isinstance(glances_id, int) else 0


def _num(value: Any) -> float | None:
    """Coerce a metric to a finite number, or None if unavailable.

    Glances and nvidia-smi report None (or "[N/A]") for metrics they can't read
    (e.g. the Tesla P40's utilization/temperature via Glances). We preserve that
    as None — a genuine gap — rather than crashing on round()/comparison/division
    or faking a misleading 0. bool is rejected so True/False can't pose as 1/0,
    and non-finite floats are dropped so json.dumps never emits invalid NaN/Inf.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _round(value: Any, ndigits: int) -> float | None:
    """Round a metric, preserving None (unavailable) as None. Never raises."""
    value = _num(value)
    return round(value, ndigits) if value is not None else None


def _dedupe_fs_by_device(fs_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse filesystem entries that are duplicate views of one backing device.

    A bind mount (e.g. systemd PrivateTmp's /tmp and /var/tmp, which sit on the
    root fs) appears as its own mount entry, but statvfs reports the underlying
    filesystem: same device_name (psutil reads /proc/mounts, which shows the
    source device with no subpath) and identical size/used as the parent mount.
    Two entries are the same physical filesystem only when resolved device AND
    size match and used agrees to within 0.1% of size; among those we keep the
    shortest real mount path ('/' over '/tmp', non-empty preferred, lexicographic
    tie-break). The tolerance is for a busy fs whose `used` moves by a few blocks
    between Glances' statvfs of the two mounts; an exact match let '/' and '/tmp'
    both through now and then. The size/used check keeps genuinely distinct
    same-device mounts separate. Entries lacking a device_name are kept as-is.
    Order is preserved.
    """

    def _key(fs: dict[str, Any]) -> tuple[str, Any, Any]:
        size, used = fs.get("size"), fs.get("used")
        if isinstance(size, (int, float)) and isinstance(used, (int, float)) and size > 0:
            used = round(used / (size * 0.001))  # bucket: 0.1% of the fs
        return (os.path.realpath(fs["device_name"]), size, used)

    def _rank(mnt: str) -> tuple[bool, int, str]:
        return (mnt == "", len(mnt), mnt)  # non-empty first, then shortest

    winner: dict[tuple[str, Any, Any], str] = {}
    for fs in fs_entries:
        if not fs.get("device_name"):
            continue
        mnt = fs.get("mnt_point") or ""
        key = _key(fs)
        if key not in winner or _rank(mnt) < _rank(winner[key]):
            winner[key] = mnt

    kept: list[dict[str, Any]] = []
    used: set[tuple[str, Any, Any]] = set()
    for fs in fs_entries:
        if not fs.get("device_name"):
            kept.append(fs)  # nothing to dedupe against
            continue
        key = _key(fs)
        if key not in used and (fs.get("mnt_point") or "") == winner[key]:
            used.add(key)
            kept.append(fs)
    return kept


def compute_stats(samples: list[Any]) -> dict[str, float | None]:
    """min/avg/max over the finite samples, or all-None when there are none.

    None/non-finite samples are dropped (an unreadable sample is not a zero); an
    empty window yields None stats (unknown), never a misleading {0, 0, 0}. A real
    idle sample of 0.0 is kept and averages normally.
    """
    vals = [v for v in (_num(s) for s in samples) if v is not None]
    if not vals:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(vals) / len(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def aggregate_cpu_cores(
    percpu_samples: dict[int, list[Any]],
) -> dict[str, float] | None:
    """Cross-core spread: min/median/max across per-core burst averages.

    ``percpu_samples`` maps core index -> that core's ``total`` samples over the
    burst. Each core is first averaged over the burst (dropping None/non-finite
    samples via ``_num``); the spread is then taken across those per-core averages.
    Returns None when no core had any finite sample (so the caller omits the key).
    """
    core_avgs: list[float] = []
    for samples in percpu_samples.values():
        vals = [v for v in (_num(s) for s in samples) if v is not None]
        if vals:
            core_avgs.append(sum(vals) / len(vals))
    if not core_avgs:
        return None
    return {
        "min": round(min(core_avgs), 2),
        "median": round(statistics.median(core_avgs), 2),
        "max": round(max(core_avgs), 2),
    }


def _gpu_mem_fields(st: dict[str, Any]) -> dict[str, float | None]:
    """Memory fields from an nvidia-smi stats dict, preserving None."""
    used_mb = _num(st.get("memory_used_mb"))
    total_mb = _num(st.get("memory_total_mb"))
    used_gb = _round(used_mb / 1024, 2) if used_mb is not None else None
    total_gb = _round(total_mb / 1024, 2) if total_mb is not None else None
    percent = (
        _round(used_mb / total_mb * 100, 1)
        if used_mb is not None and total_mb
        else None
    )
    return {"mem_used_gb": used_gb, "mem_total_gb": total_gb, "mem_percent": percent}


def build_gpu_records(
    gpu_stats: list[dict[str, Any]], gpu_data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Assemble per-GPU history records (utilization + memory + thermal slowdown).

    nvidia-smi (``gpu_stats``) is the primary source — it reads GPUs that
    Glances' pynvml binding reports as null (some datacenter GPUs, e.g. the Tesla P40). Glances ``proc``
    is a per-index fallback for utilization when nvidia-smi has none. Unreadable
    fields stay None (never faked 0); a GPU without an index is dropped rather
    than collapsed into "GPU 0". Pure (no I/O) so the null behaviour is testable.
    """
    glances_util: dict[Any, float | None] = {
        _parse_gpu_index(g): _num(g.get("proc")) for g in (gpu_data or [])
    }

    gpus: list[dict[str, Any]] = []
    if gpu_stats:
        for st in gpu_stats:
            idx = st.get("index")
            if idx is None:
                continue
            util = _num(st.get("util_percent"))
            if util is None:  # nvidia-smi had no util; try Glances for this GPU
                util = glances_util.get(idx)
            sw_us = _num(st.get("throttle_sw_thermal_us"))
            hw_us = _num(st.get("throttle_hw_thermal_us"))
            gpus.append(
                {
                    "index": idx,
                    "util_percent": _round(util, 1),
                    **_gpu_mem_fields(st),
                    # Seconds of thermal slowdown since driver load (cumulative:
                    # a rise between two records is slowdown in that interval).
                    "throttle_sw_thermal_s": _round(sw_us / 1e6, 1) if sw_us is not None else None,
                    "throttle_hw_thermal_s": _round(hw_us / 1e6, 1) if hw_us is not None else None,
                }
            )
    elif gpu_data:
        # nvidia-smi unavailable: Glances utilization only, memory unknown.
        for gpu in gpu_data:
            gpus.append(
                {
                    "index": _parse_gpu_index(gpu),
                    "util_percent": _round(gpu.get("proc"), 1),
                    "mem_used_gb": None,
                    "mem_total_gb": None,
                    "mem_percent": None,
                    "throttle_sw_thermal_s": None,
                    "throttle_hw_thermal_s": None,
                }
            )
    return gpus


def gpu_temps(
    gpu_stats: list[dict[str, Any]], gpu_data: list[dict[str, Any]]
) -> dict[str, float | None]:
    """{'GPU N': temperature|None, 'GPU N mem': temperature} — nvidia-smi primary, Glances fallback.

    Emits the core key even when temperature is None so the temps chart shows a
    gap for that GPU rather than dropping the series. 'GPU N mem' (memory/HBM,
    nvidia-smi only) is emitted only when readable: a GDDR card (the P40) never
    has one, and a permanent all-gap series would be noise.
    """
    temps: dict[str, float | None] = {}
    if gpu_stats:
        for st in gpu_stats:
            idx = st.get("index")
            if idx is not None:
                temps[f"GPU {idx}"] = _round(st.get("temperature"), 1)
                if st.get("temperature_memory") is not None:
                    temps[f"GPU {idx} mem"] = _round(st.get("temperature_memory"), 1)
    elif gpu_data:
        for gpu in gpu_data:
            temps[f"GPU {_parse_gpu_index(gpu)}"] = _round(gpu.get("temperature"), 1)
    return temps


def aggregate_ping(
    ping_samples: dict[str, list[Any]], ping_attempts: dict[str, int]
) -> dict[str, Any]:
    """Per-target ping stats plus the fraction of the burst that was lost.

    Each target entry is {avg,min,max,loss} when at least one ping succeeded
    (stats over the successes, loss = failed/attempts), or
    {avg:None,min:None,max:None,loss:1.0} when every attempt timed out. A missing
    target key means "never measured" (ping disabled), distinct from a full
    outage. Pure (no I/O) so the behaviour is unit-testable.
    """
    result: dict[str, Any] = {}
    for target, attempts in ping_attempts.items():
        vals = [v for v in (_num(s) for s in ping_samples.get(target, [])) if v is not None]
        if vals:
            failed = max(0, attempts - len(vals))
            loss = round(failed / attempts, 4) if attempts else 0.0
            result[target] = {**compute_stats(vals), "loss": loss}
        else:
            result[target] = {"avg": None, "min": None, "max": None, "loss": 1.0}
    return result


CPUFREQ_DIR: Final[Path] = Path("/sys/devices/system/cpu/cpufreq")


def _read_cpu_freqs() -> list[float]:
    """Current clock of every cpufreq policy (one per logical core on most boxes), in MHz.

    ``scaling_cur_freq`` is the kernel's last known frequency and world-readable
    (``cpuinfo_cur_freq`` is root-only). A box without cpufreq (most VMs) has no
    policies and yields [], so the caller omits the record key. Sync: thread pool.
    """
    freqs: list[float] = []
    for path in CPUFREQ_DIR.glob("policy*/scaling_cur_freq"):
        try:
            freqs.append(int(path.read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return freqs


def _cpu_freq_limits() -> dict[str, float | None]:
    """The hardware clock range (MHz) from policy0, None when unreadable."""
    out: dict[str, float | None] = {}
    for key, name in (("min_mhz", "cpuinfo_min_freq"), ("max_mhz", "cpuinfo_max_freq")):
        try:
            out[key] = int((CPUFREQ_DIR / "policy0" / name).read_text().strip()) / 1000
        except (OSError, ValueError):
            out[key] = None
    return out


def aggregate_cpu_freq(
    freq_samples: list[float], limits: dict[str, float | None] | None = None
) -> dict[str, float | None] | None:
    """min/median/avg/max MHz over every (core, sample) reading of the burst.

    Per-core readings are pooled rather than averaged first: the point is to see
    whether some cores sit at idle clocks while others boost, which an all-core
    mean (Glances' quicklook ``cpu_hz_current``) blurs away. None when nothing
    was readable, so the caller omits the key; the hardware range rides along.
    """
    vals = sorted(v for v in (_num(s) for s in freq_samples) if v is not None)
    if not vals:
        return None
    return {
        "min": round(vals[0], 1),
        "median": round(statistics.median(vals), 1),
        "avg": round(sum(vals) / len(vals), 1),
        "max": round(vals[-1], 1),
        **(limits or {}),
    }


_CLK_TCK: Final[int] = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read_boot_time() -> int | None:
    """Boot time (epoch s) from /proc/stat ``btime``; None off Linux."""
    try:
        return next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    except (OSError, ValueError, IndexError, StopIteration):
        return None


# Constant for the life of the process: read once, not per process row.
_BOOT_TIME: Final[int | None] = _read_boot_time()


def _proc_parent_and_start(pid: int) -> tuple[int | None, float | None]:
    """(ppid, create_time epoch s) from /proc/<pid>/stat, (None, None) when gone.

    Lets a pid be tied to a process tree after the fact (e.g. which coding agent
    launched it): pids are recycled, so the start time disambiguates. Field 22 is
    the start in clock ticks since boot, added to ``_BOOT_TIME``.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The comm field is in parentheses and may itself contain spaces/parens.
        fields = stat[stat.rindex(")") + 2:].split()
        ppid = int(fields[1])
        start_ticks = int(fields[19])
        create_time = (
            round(_BOOT_TIME + start_ticks / _CLK_TCK, 2) if _BOOT_TIME is not None else None
        )
        return ppid, create_time
    except (OSError, ValueError, IndexError):
        return None, None


def build_process_records(
    processes: list[Any], gpu_processes: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """The per-process slice of a history record (sync: reads /proc for names).

    Keeps the top ``count`` processes by CPU and the top ``count`` by RSS (the
    union, so between ``count`` and ``2*count`` rows) plus every process holding
    GPU memory, with the sum of its memory over the GPUs it uses and their
    indices. A GPU process missing from Glances' list (another pid namespace)
    still gets a row, with its CPU/RSS unknown. Names are the sanitized display
    name (executable / module / script, never the arguments), like /api/processlist.
    ``ppid`` and ``create_time`` (epoch seconds) come from /proc so a row can be
    attributed to a process tree later. Disk rates come from Glances' cumulative
    ``io_counters`` over its own refresh interval. Rows are sparse: a key whose
    value is unknown (None) or a rate that is zero is left out, so a row carries
    only ``pid`` and ``name`` for sure. Sorted by CPU, GPU-only rows last.
    """
    gpu_by_pid: dict[int, dict[str, Any]] = {}
    for g in gpu_processes:
        pid = g.get("pid")
        if not isinstance(pid, int):
            continue
        slot = gpu_by_pid.setdefault(pid, {"mem": 0.0, "gpus": set(), "name": g.get("name")})
        slot["mem"] += _num(g.get("gpu_memory_mib")) or 0.0
        if isinstance(g.get("gpu_index"), int):
            slot["gpus"].add(g["gpu_index"])

    valid = [
        p for p in processes
        if isinstance(p, dict) and isinstance(p.get("pid"), int) and not isinstance(p["pid"], bool)
    ]

    def cpu(p: dict[str, Any]) -> float:
        return _num(p.get("cpu_percent")) or 0.0

    def rss_bytes(p: dict[str, Any]) -> float | None:
        info = p.get("memory_info")
        return _num(info.get("rss")) if isinstance(info, dict) else None

    def rss(p: dict[str, Any]) -> float:
        return rss_bytes(p) or 0.0

    def disk_kbps(p: dict[str, Any]) -> tuple[float | None, float | None]:
        # Glances: io_counters = [read, write, read_old, write_old, io_tag] cumulative
        # bytes, io_tag 1 when readable; the rate is over time_since_update.
        io = p.get("io_counters")
        dt = _num(p.get("time_since_update"))
        if not (isinstance(io, list) and len(io) >= 5 and io[4] == 1 and dt):
            return None, None
        vals = [_num(x) for x in io[:4]]
        if any(v is None for v in vals):
            return None, None
        return (vals[0] - vals[2]) / dt / 1024, (vals[1] - vals[3]) / dt / 1024

    def sparse(row: dict[str, Any], rates: tuple[str, ...] = ()) -> dict[str, Any]:
        # Drop unknowns, and the rate keys when zero or negative (counter reset).
        return {
            k: v for k, v in row.items()
            if v is not None and not (k in rates and isinstance(v, (int, float)) and v <= 0)
        }

    chosen: dict[int, dict[str, Any]] = {}
    for p in sorted(valid, key=cpu, reverse=True)[:count]:
        chosen.setdefault(p["pid"], p)
    for p in sorted(valid, key=rss, reverse=True)[:count]:
        chosen.setdefault(p["pid"], p)
    for p in valid:
        if p["pid"] in gpu_by_pid:
            chosen.setdefault(p["pid"], p)

    rows: list[dict[str, Any]] = []
    for pid, p in chosen.items():
        info = get_sanitized_process_info(pid, str(p.get("name") or "unknown"))
        ppid, create_time = _proc_parent_and_start(pid)
        rd, wr = disk_kbps(p)
        row: dict[str, Any] = {
            "pid": pid,
            "ppid": ppid,
            "create_time": create_time,
            "name": info["name"],
            "user": p.get("username"),
            "status": p.get("status"),
            "cpu_percent": _round(p.get("cpu_percent"), 1),
            "rss_mb": _round(rss_bytes(p) / (1024**2), 1) if rss_bytes(p) is not None else None,
            "threads": p.get("num_threads") if isinstance(p.get("num_threads"), int) else None,
            "disk_read_kbps": _round(rd, 1),
            "disk_write_kbps": _round(wr, 1),
        }
        gpu = gpu_by_pid.get(pid)
        if gpu and gpu["mem"] > 0:
            row["gpu_mem_mib"] = round(gpu["mem"], 1)
            row["gpus"] = sorted(gpu["gpus"])
        rows.append(sparse(row, ("disk_read_kbps", "disk_write_kbps")))
    # GPU processes Glances did not list (the join caveat of the live view)
    for pid, gpu in gpu_by_pid.items():
        if pid in chosen:
            continue
        ppid, create_time = _proc_parent_and_start(pid)
        rows.append(
            sparse(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "create_time": create_time,
                    "name": gpu["name"] or "unknown",
                    "gpu_mem_mib": round(gpu["mem"], 1),
                    "gpus": sorted(gpu["gpus"]),
                }
            )
        )
    rows.sort(key=lambda r: ("cpu_percent" not in r, -r.get("cpu_percent", 0.0)))
    return rows


async def collect_history_sample(glances_url: str, live: bool = False) -> dict[str, Any]:
    """Collect a single history sample from all data sources.

    Takes samples over the configured duration and computes min/avg/max
    for volatile metrics. ``live`` (the /text snapshot) takes one immediate
    sample with no process list or IP lookup, so a request returns in about a
    second instead of a whole sample window, and never calls out to ipify.
    """
    sample_duration: int = 0 if live else APP.state.history_sample_duration
    sample_count: int = 1 if live else APP.state.history_sample_count
    sample_interval: float = sample_duration / max(1, sample_count)

    cpu_samples: list[float] = []
    # Per-core CPU usage across the burst, keyed by core index: {idx: [total, ...]}
    percpu_samples: dict[int, list[Any]] = {}
    net_in_samples: list[float] = []
    net_out_samples: list[float] = []
    # Disk I/O, one dict per sample iteration: {disk_name: (read_mbps, write_mbps)}.
    # Keyed per iteration (not per disk) so a device that appears or vanishes
    # mid-window cannot shift another device's samples out of alignment.
    disk_iter_samples: list[dict[str, tuple[float, float]]] = []
    # Per-core clocks, pooled over the burst (see aggregate_cpu_freq).
    freq_samples: list[float] = []
    processes_enabled: bool = APP.state.history_processes_enabled and not live
    process_count: int = APP.state.history_process_count
    ping_enabled: bool = APP.state.ping_enabled
    ping_samples: dict[str, list[float]] = (
        {t: [] for t in PING_TARGETS} if ping_enabled else {}
    )
    ping_attempts: dict[str, int] = (
        {t: 0 for t in PING_TARGETS} if ping_enabled else {}
    )

    # Collect samples over time
    async with httpx.AsyncClient() as client:
        for i in range(sample_count):
            try:
                # Fetch CPU
                cpu_resp = await client.get(f"{glances_url}/api/4/cpu", timeout=5.0)
                if cpu_resp.status_code == 200:
                    cpu_data: Any = cpu_resp.json()
                    # Raw value (may be None if Glances reports null); compute_stats
                    # drops non-numeric samples so this never crashes the record.
                    cpu_samples.append(cpu_data.get("total"))

                # Fetch per-core CPU (for the cross-core spread record). Keyed by
                # enumerate index to match the live per-core chart (app.js).
                percpu_resp = await client.get(
                    f"{glances_url}/api/4/percpu", timeout=5.0
                )
                if percpu_resp.status_code == 200:
                    percpu_data: Any = percpu_resp.json()
                    for idx, core in enumerate(percpu_data):
                        percpu_samples.setdefault(idx, []).append(core.get("total"))

                # Per-core clock frequencies (sysfs; [] without cpufreq)
                freq_samples.extend(await asyncio.to_thread(_read_cpu_freqs))

                # Fetch network I/O
                net_resp = await client.get(f"{glances_url}/api/4/network", timeout=5.0)
                if net_resp.status_code == 200:
                    net_data: Any = net_resp.json()
                    # Filter out loopback - check interface_name field (Glances uses this)
                    total_in = sum(
                        iface.get("bytes_recv_rate_per_sec", 0) or 0
                        for iface in net_data
                        if not (iface.get("interface_name") or "").startswith("lo")
                    )
                    total_out = sum(
                        iface.get("bytes_sent_rate_per_sec", 0) or 0
                        for iface in net_data
                        if not (iface.get("interface_name") or "").startswith("lo")
                    )
                    net_in_samples.append(
                        total_in / 1e6
                    )  # Convert to MB/s (match live view)
                    net_out_samples.append(total_out / 1e6)
                else:
                    logger.warning("Network API returned status %d", net_resp.status_code)

                # Fetch disk I/O
                disk_resp = await client.get(f"{glances_url}/api/4/diskio", timeout=5.0)
                if disk_resp.status_code == 200:
                    disk_data: Any = disk_resp.json()
                    # Per-drive disk I/O; the whole-box totals are summed from
                    # these after the loop, once the mounted devices are known.
                    this_iter: dict[str, tuple[float, float]] = {}
                    for disk in disk_data:
                        disk_name: Any = disk.get("disk_name")
                        if disk_name:
                            read_mbps = (
                                disk.get("read_bytes_rate_per_sec", 0) or 0
                            ) / 1e6
                            write_mbps = (
                                disk.get("write_bytes_rate_per_sec", 0) or 0
                            ) / 1e6
                            this_iter[disk_name] = (read_mbps, write_mbps)
                    disk_iter_samples.append(this_iter)
                else:
                    logger.warning(
                        "Disk IO API returned status %d", disk_resp.status_code
                    )

            except Exception as e:
                # TODO: track which metrics failed and include in record for frontend to indicate partial data
                logger.warning("Sample %d collection error: %s", i, e)

            # Ping (runs in thread pool, concurrently with sleep below)
            if ping_enabled:
                # Count an attempt per target up front so a gather-timeout (which
                # skips the append below) still counts as a loss, not a non-event.
                for target in PING_TARGETS:
                    ping_attempts[target] += 1
                try:
                    ping_results = await asyncio.wait_for(
                        asyncio.gather(
                            *(asyncio.to_thread(_run_ping, t) for t in PING_TARGETS)
                        ),
                        timeout=8,
                    )
                    for target, latency in zip(PING_TARGETS, ping_results):
                        if latency is not None:
                            ping_samples[target].append(latency)
                except Exception as e:
                    logger.warning("Ping sample %d failed: %s", i, e)

            # Wait before next sample (except for last)
            if i < sample_count - 1:
                await asyncio.sleep(sample_interval)

        # Fetch point-in-time metrics (only need one sample)
        memory_data: dict = {}
        swap_data: dict = {}
        gpu_data: list = []
        temps_data: list = []
        load_data: dict = {}

        try:
            mem_resp = await client.get(f"{glances_url}/api/4/mem", timeout=5.0)
            if mem_resp.status_code == 200:
                memory_data = mem_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch mem: %s", e)

        try:
            swap_resp = await client.get(f"{glances_url}/api/4/memswap", timeout=5.0)
            if swap_resp.status_code == 200:
                swap_data = swap_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch memswap: %s", e)

        try:
            gpu_resp = await client.get(f"{glances_url}/api/4/gpu", timeout=5.0)
            if gpu_resp.status_code == 200:
                gpu_data = gpu_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch gpu: %s", e)

        try:
            temps_resp = await client.get(f"{glances_url}/api/4/sensors", timeout=5.0)
            if temps_resp.status_code == 200:
                temps_data = temps_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch sensors: %s", e)

        try:
            load_resp = await client.get(f"{glances_url}/api/4/load", timeout=5.0)
            if load_resp.status_code == 200:
                load_data = load_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch load: %s", e)

        # Process list, only when per-process history is on: Glances' point-in-time
        # view (cpu_percent is over its own refresh interval), sanitized below.
        process_data: list = []
        if processes_enabled:
            try:
                proc_resp = await client.get(f"{glances_url}/api/4/processlist", timeout=10.0)
                if proc_resp.status_code == 200:
                    process_data = proc_resp.json()
            except Exception as e:
                logger.warning("History: failed to fetch processlist: %s", e)

        # Fetch filesystem data for drive usage
        fs_data: list = []
        try:
            fs_resp = await client.get(f"{glances_url}/api/4/fs", timeout=5.0)
            if fs_resp.status_code == 200:
                # De-dupe duplicate-device entries (e.g. PrivateTmp /tmp, /var/tmp
                # bind mounts mirroring /) so history drives + per-drive I/O labels
                # match the live view. This fetch bypasses the proxy, so dedupe here.
                fs_data = _dedupe_fs_by_device(fs_resp.json())
        except Exception as e:
            logger.warning("History: failed to fetch fs: %s", e)

    # Fetch IP info (point-in-time)
    ip_data: dict[str, Any] = {}
    try:
        if not live:
            ip_data = await asyncio.to_thread(_get_ip_info)
    except Exception as e:
        logger.warning("History: failed to fetch IP info: %s", e)

    # Build the record
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_sec": sample_duration,
    }

    def _gb(value: Any) -> float | None:
        """bytes -> GB, preserving None (unavailable) rather than dividing None."""
        value = _num(value)
        return round(value / (1024**3), 2) if value is not None else None

    # CPU (with min/avg/max)
    if cpu_samples:
        record["cpu"] = compute_stats(cpu_samples)

    # CPU core spread: min/median/max across cores. Distinguishes "one core pinned"
    # from "all cores evenly loaded" — the total-only "cpu" stat cannot. Omitted
    # when /api/4/percpu is never readable.
    cpu_cores = aggregate_cpu_cores(percpu_samples)
    if cpu_cores is not None:
        record["cpu_cores"] = cpu_cores

    # CPU clocks: min/median/avg/max MHz over every core and sample of the burst,
    # with the hardware range. Omitted on a box without cpufreq.
    if freq_samples:
        cpu_freq = aggregate_cpu_freq(freq_samples, await asyncio.to_thread(_cpu_freq_limits))
        if cpu_freq is not None:
            record["cpu_freq"] = cpu_freq

    # Memory (point-in-time)
    if memory_data:
        record["memory"] = {
            "used_gb": _gb(memory_data.get("used")),
            "total_gb": _gb(memory_data.get("total")),
            "percent": _round(memory_data.get("percent"), 1),
        }

    # Swap (point-in-time)
    if swap_data and (_num(swap_data.get("total")) or 0) > 0:
        record["swap"] = {
            "used_gb": _gb(swap_data.get("used")),
            "total_gb": _gb(swap_data.get("total")),
            "percent": _round(swap_data.get("percent"), 1),
        }

    # Network I/O (with stats)
    if net_in_samples or net_out_samples:
        record["network"] = {
            "in_mbps": compute_stats(net_in_samples),
            "out_mbps": compute_stats(net_out_samples),
        }

    # Disk I/O (with stats). Mapping from disk name to mount point, same logic
    # as app.js: resolve symlinks (e.g. /dev/mapper/xxx -> /dev/dm-0).
    # One name per device: the shortest mount point ('/' over '/tmp'). Under the
    # systemd unit the root fs is also visible as PrivateTmp's /tmp and /var/tmp
    # and the ReadWritePaths bind mount; when the dedupe below did not merge
    # them (their `used` differed by a block between two statvfs calls) a
    # last-writer-wins dict labelled the root drive '/tmp' in some records.
    disk_to_mount: dict[str, str] = {}
    for fs in fs_data:
        device_name: str = fs.get("device_name", "")
        mnt_point: str = fs.get("mnt_point", "")
        if device_name and mnt_point:
            resolved: str = os.path.realpath(device_name)
            if resolved.startswith("/dev/"):
                disk = resolved[5:]
                if disk not in disk_to_mount or len(mnt_point) < len(disk_to_mount[disk]):
                    disk_to_mount[disk] = mnt_point
    # The whole-box totals count the MOUNTED devices only: Glances' diskio rows are
    # one per block device including partitions and md/dm layers, so summing every
    # row counts a write on an md mirror three times (member, member, md). Without
    # fs data (Glances could not read the mounts) every row is counted, as before.
    seen_disks: set[str] = {d for it in disk_iter_samples for d in it}
    counted: set[str] = {d for d in seen_disks if d in disk_to_mount} or seen_disks
    disk_read_samples = [sum(v[0] for d, v in it.items() if d in counted) for it in disk_iter_samples]
    disk_write_samples = [sum(v[1] for d, v in it.items() if d in counted) for it in disk_iter_samples]
    if disk_read_samples or disk_write_samples:
        record["disk_io"] = {
            "read_mbps": compute_stats(disk_read_samples),
            "write_mbps": compute_stats(disk_write_samples),
        }

        # Per-drive disk I/O (only for mounted filesystems, like live chart)
        drives_io: list[dict[str, Any]] = []
        for disk_name in sorted(seen_disks & disk_to_mount.keys()):
            samples = [it[disk_name] for it in disk_iter_samples if disk_name in it]
            drives_io.append(
                {
                    "name": disk_to_mount[disk_name],  # Use mount point as name
                    "read_mbps": compute_stats([s[0] for s in samples]),
                    "write_mbps": compute_stats([s[1] for s in samples]),
                }
            )
        if drives_io:
            record["disk_io"]["drives"] = drives_io

    # Drive usage (filesystem, point-in-time)
    if fs_data:
        drives: list[dict[str, Any]] = []
        for fs in fs_data:
            mnt_point = fs.get("mnt_point")
            if mnt_point:
                drives.append(
                    {
                        "mount": mnt_point,
                        "used_gb": _gb(fs.get("used")),
                        "total_gb": _gb(fs.get("size")),
                        "percent": _round(fs.get("percent"), 1),
                    }
                )
        if drives:
            record["drives"] = drives

    # GPUs (point-in-time). Sourced from nvidia-smi (Glances reports some datacenter
    # GPUs as all-null); Glances utilization is a fallback. Unreadable fields stay None.
    # Offloaded: get_gpu_stats() shells out to nvidia-smi (up to ~5s), which would
    # otherwise block the event loop for the whole recording cycle.
    gpu_stats: list[dict[str, Any]] = await asyncio.to_thread(get_gpu_stats)
    gpus = build_gpu_records(gpu_stats, gpu_data)
    if gpus:
        record["gpus"] = gpus

    # Temperatures (point-in-time): CPU/board sensors from Glances, plus GPU
    # temps from nvidia-smi (merged into the same chart as the live view).
    temps: dict[str, float | None] = {}
    # Power (point-in-time, watts): the BMC's system draw from the collector
    # snapshot plus each GPU's draw from nvidia-smi.
    power: dict[str, float | None] = {}
    for sensor in temps_data:
        # Only include temperature sensors (exclude battery, fan speed, etc.)
        if sensor.get("type") != "temperature_core":
            continue
        label: str = sensor.get("label", "")
        value = _round(sensor.get("value"), 1)
        if label and value is not None:
            # Use label as key, handling duplicates by keeping max
            temps[label] = max(temps[label], value) if label in temps else value
    temps.update(gpu_temps(gpu_stats, gpu_data))
    # Chassis sensors from the collector's 60 s sensors snapshot (IPMI and RAPL are
    # root-only, so the OS cannot see them). Only a fresh snapshot counts: a dead
    # collector then leaves gaps in the charts instead of a flat line forever.
    sensors = await asyncio.to_thread(_get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC)
    if sensors["status"] == "ok":
        temps.update(
            {r["label"]: r["value"] for r in sensors.get("temps", []) if r.get("value") is not None}
        )
        fans = {r["label"]: r["value"] for r in sensors.get("fans", []) if r.get("value") is not None}
        if fans:
            record["fans"] = fans
        # Watts rows only (BMC total + RAPL CPU/DRAM): the same capture also carries PSU amps/volts.
        power.update(
            {
                r["label"]: r["value"]
                for r in sensors.get("power", [])
                if r.get("value") is not None and str(r.get("reading", "")).endswith("Watts")
            }
        )
    # Drive temperatures from the 15-min snapshot (SMART), fresh only, keyed by device.
    health = await asyncio.to_thread(_get_storage_health)
    if health["status"] == "ok":
        temps.update(
            {
                d["device"]: d["temperature_c"]
                for d in health.get("drives", [])
                if isinstance(d, dict) and d.get("device") and d.get("temperature_c") is not None
            }
        )
    if temps:
        record["temps"] = temps
    for st in gpu_stats:
        if st.get("index") is not None and st.get("power_draw") is not None:
            power[f"GPU {st['index']}"] = _round(st["power_draw"], 1)
    if power:
        record["power"] = power

    # Load (point-in-time)
    if load_data:
        record["load"] = {
            "min1": _round(load_data.get("min1"), 2),
            "min5": _round(load_data.get("min5"), 2),
            "min15": _round(load_data.get("min15"), 2),
        }

    # Top processes (point-in-time), only when enabled. GPU memory per process
    # comes from nvidia-smi; a box without it just gets no GPU fields. Offloaded:
    # nvidia-smi shells out and the sanitizer reads /proc per process.
    # Like every other section, a failure here drops only this key: the record's
    # cpu/memory/... must never be lost to one unreadable process.
    if processes_enabled and isinstance(process_data, list) and process_data:
        gpu_procs: list[dict[str, Any]] = []
        try:
            gpu_procs = await asyncio.to_thread(_get_gpu_processes)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("History: failed to read GPU processes: %s", e)
        try:
            record["processes"] = await asyncio.to_thread(
                build_process_records, process_data, gpu_procs, process_count
            )
        except Exception as e:
            logger.warning("History: failed to build process records: %s", e)

    # Ping. A target with zero successful samples is recorded as None (all
    # attempts timed out) rather than omitted, so a failure is distinguishable
    # from "never measured" — see aggregate_ping.
    if ping_samples:
        record["ping"] = aggregate_ping(ping_samples, ping_attempts)

    # IP addresses (point-in-time)
    if ip_data:
        record["ip"] = ip_data

    return record


_history_write_lock = threading.Lock()


def append_history_record(record: dict[str, Any]) -> None:
    """Append a history record to the JSONL file.

    The scheduled recorder and a manual /api/history/record both call this from
    worker threads; the lock keeps two records from interleaving into one torn
    line (a record over the 8 KiB text buffer is more than one write()).
    """
    LOGS_DIR.mkdir(exist_ok=True)
    with _history_write_lock, open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
    logger.info("History record saved: %s", record.get("timestamp"))


def missing_core_metrics(record: dict[str, Any]) -> list[str]:
    """Core metric groups that are absent, or present but entirely null.

    Presence of the key is not evidence the sample worked: a Glances brownout
    returning null totals still yields ``cpu_samples == [None, ...]``, which is a
    truthy list, so ``record["cpu"]`` gets written as ``{"avg": None, ...}``.
    Callers use this to avoid reporting a near-empty record as a healthy one.
    """
    missing: list[str] = []
    cpu = record.get("cpu")
    if not isinstance(cpu, dict) or cpu.get("avg") is None:
        missing.append("cpu")
    memory = record.get("memory")
    if not isinstance(memory, dict) or memory.get("percent") is None:
        missing.append("memory")
    return missing


def _apply_record_health(record: dict[str, Any]) -> None:
    """Update the recording-health state from a just-written record.

    A partial record deliberately does NOT refresh ``history_last_success``: the
    health block derives ``stale`` purely from that timestamp's age, so letting a
    near-empty record refresh it would keep reporting "healthy" right through a
    Glances outage. Withholding it lets ``stale`` trip on schedule, and
    ``last_error`` supplies the reason.
    """
    missing = missing_core_metrics(record)
    if missing:
        APP.state.history_last_error = f"partial record, missing {missing}"
        logger.warning("History record is partial, missing %s", missing)
    else:
        APP.state.history_last_success = record.get("timestamp")
        APP.state.history_last_error = None


async def history_recorder() -> None:
    """Background task that records metrics at the configured interval."""
    # Wait for initial interval before first recording
    await asyncio.sleep(APP.state.history_record_interval)

    while True:
        try:
            glances_url = APP.state.glances_url
            record = await collect_history_sample(glances_url)
            await asyncio.to_thread(append_history_record, record)
            _apply_record_health(record)
        except Exception as e:
            # Surfaced to the frontend via /api/history's "recording" block so a
            # stalled recorder is visible instead of silently rotting for days.
            APP.state.history_last_error = str(e)
            logger.error("History recording failed: %s", e)

        await asyncio.sleep(APP.state.history_record_interval)


async def wait_for_glances_warm(glances_url: str, timeout: float = 20.0) -> bool:
    """Wait until Glances' CPU readings are real.

    A fresh Glances returns garbage for its first two refresh cycles (~4 s):
    cycle 1 has a plausible total but per-core values measured over milliseconds
    (median ~87%), cycle 2 has total 0.0 and every core at 100%. A sample taken
    then records cpu.min 0.0 and a cores median of ~45% on an idle box. Each
    plugin warms up on its own first requests, so poll cpu and percpu together
    until the cpu total has changed twice (two refreshes have passed) and is > 0.
    Returns False on timeout (the caller samples anyway).
    """
    deadline = time.monotonic() + timeout
    last: float | None = None
    changes = 0
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{glances_url}/api/4/cpu", timeout=5.0)
                total = _num(resp.json().get("total")) if resp.status_code == 200 else None
                # Per-core warms up separately, on its own first requests.
                await client.get(f"{glances_url}/api/4/percpu", timeout=5.0)
            except Exception:
                total = None
            if total is not None:
                if last is not None and total != last:
                    changes += 1
                last = total
                if changes >= 2 and total > 0:
                    return True
            await asyncio.sleep(0.5)
    return False


async def record_initial_sample(glances_url: str) -> None:
    """Record initial history sample on startup (runs in background)."""
    try:
        if not await wait_for_glances_warm(glances_url):
            logger.warning("Glances CPU readings still cold after 20 s; recording anyway")
        logger.info("Recording initial history sample")
        initial_sample = await collect_history_sample(glances_url)
        await asyncio.to_thread(append_history_record, initial_sample)
        _apply_record_health(initial_sample)
    except Exception as e:
        APP.state.history_last_error = str(e)
        logger.warning("Failed to record startup history: %s", e)


async def check_disconnected_clients() -> None:
    """Background task (every 5 s): restart Glances if it died, and log clients
    inactive for 30+ seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            # systemd only restarts the unit when THIS process exits; a dead Glances
            # would otherwise leave every card 502 and the history partial for good.
            # At most one restart a minute: a Glances that cannot start at all must
            # not be respawned every 5 s forever.
            glances = APP.state.glances_process
            if glances.poll() is not None and time.time() - APP.state.glances_started_at >= 60:
                logger.error("Glances exited with code %s; restarting (see logs/glances.log)", glances.returncode)
                APP.state.glances_process = start_glances(APP.state.glances_port)
                APP.state.glances_started_at = time.time()
            now: float = time.time()
            disconnected: list[str] = [
                ip for ip, ts in client_last_seen.items() if now - ts > CLIENT_TIMEOUT
            ]
            for ip in disconnected:
                del client_last_seen[ip]
                logger.info("Client disconnected: %s", ip)
        except Exception:
            # An unhandled exception would end this task silently, and with it
            # every future Glances restart. Log it and keep ticking.
            logger.exception("watchdog tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage Glances subprocess lifecycle and background tasks."""
    glances_port: int = app.state.glances_port

    # Startup
    # Enforce 0600 on the password hash even if it was created/edited another way
    # (a group- or world-readable .password leaks the PBKDF2 hash for offline attack).
    if PASSWORD_FILE.exists():
        try:
            PASSWORD_FILE.chmod(0o600)
        except OSError as e:
            logger.warning("Could not tighten permissions on %s: %s", PASSWORD_FILE, e)

    logger.info("Starting Glances web server on port %d...", glances_port)
    app.state.glances_process = start_glances(glances_port)
    app.state.glances_started_at = time.time()
    app.state.started_at = datetime.now(timezone.utc).isoformat()

    # Initialize mutable history recording settings
    app.state.history_record_interval = DEFAULT_HISTORY_RECORD_INTERVAL
    app.state.history_sample_duration = DEFAULT_HISTORY_SAMPLE_DURATION
    app.state.history_sample_count = DEFAULT_HISTORY_SAMPLE_COUNT
    app.state.manual_record_min_interval = DEFAULT_MANUAL_RECORD_MIN_INTERVAL
    # Recorder health, surfaced via /api/history so a stalled recorder is visible.
    app.state.history_last_success = None
    app.state.history_last_error = None
    app.state.ping_enabled = True
    app.state.history_processes_enabled = DEFAULT_HISTORY_PROCESSES_ENABLED
    app.state.history_process_count = DEFAULT_HISTORY_PROCESS_COUNT

    # Start background task for client disconnect detection
    disconnect_task = asyncio.create_task(check_disconnected_clients())

    # Start background task for history recording
    app.state.history_task = asyncio.create_task(history_recorder())
    logger.info(
        "History recording enabled (every %d seconds)",
        DEFAULT_HISTORY_RECORD_INTERVAL,
    )

    # Give Glances time to start
    await asyncio.sleep(2)

    # Record initial sample in background (non-blocking, last step of startup)
    initial_sample_task = asyncio.create_task(
        record_initial_sample(app.state.glances_url)
    )

    yield  # Server runs here

    # Shutdown (runs when Ctrl+C is pressed)
    for task in [initial_sample_task, disconnect_task, app.state.history_task]:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    stop_glances(app.state.glances_process)  # the watchdog may have replaced the original


# No /docs, /redoc, /openapi.json: nothing uses them and the service is unauthenticated.
APP: FastAPI = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def setup_logging() -> None:
    """Configure logging to write to logs/ directory."""
    LOGS_DIR.mkdir(exist_ok=True)

    log_file: Path = LOGS_DIR / "server.log"

    formatter: logging.Formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler. Rotating because this log is long-lived and a single noisy
    # failure mode can burst thousands of lines a minute; 5 x 5 MiB caps it.
    file_handler: logging.Handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Console handler
    console_handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# Mount static files
APP.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@APP.middleware("http")
async def log_connections(request: Request, call_next):
    """Log client connections (new clients or clients not seen in 30s)."""
    client_ip: str = request.client.host if request.client else "unknown"
    now: float = time.time()
    if (
        client_ip not in client_last_seen
        or now - client_last_seen[client_ip] > CLIENT_TIMEOUT
    ):
        logger.info("Client connected: %s", client_ip)
    client_last_seen[client_ip] = now
    return await call_next(request)


def _read_history(
    hours: float,
    initial_read_size: int = HISTORY_INITIAL_TAIL_READ,
    max_bytes: int = HISTORY_MAX_TAIL_READ,
) -> tuple[list[dict[str, Any]], int, bool] | str:
    """Read history records from the tail of the JSONL file (sync, called from thread pool).

    Records are appended chronologically, so instead of scanning the whole file
    we read a window from the end and grow it (doubling) until the oldest record
    in the window predates the cutoff — i.e. the window covers the requested range.
    The window never exceeds ``max_bytes``: a range whose cutoff is older than the
    oldest record in that window (e.g. "1 year" on a partial file) would otherwise
    read the whole — potentially huge — file, so we stop at the cap and report it.

    Returns (records, skipped_line_count, truncated) on success, or an error
    string on failure. ``truncated`` is True when the byte cap was hit before the
    range was covered (the oldest returned data is newer than the requested
    cutoff). Skipped lines are non-empty lines that don't parse as a dict with a
    valid timestamp — corruption worth surfacing, not silently dropping.
    """
    if not HISTORY_FILE.exists():
        return [], 0, False

    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)

    try:
        with open(HISTORY_FILE, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            read_size = initial_read_size
            while True:
                capped = min(read_size, max_bytes)
                offset = max(0, size - capped)
                f.seek(offset)
                data = f.read()
                if offset > 0:
                    # Drop the (likely partial) line the window starts in
                    nl = data.find(b"\n")
                    data = data[nl + 1 :] if nl != -1 else b""

                records: list[dict[str, Any]] = []
                first_ts: float | None = None
                skipped = 0
                for line in data.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record: dict[str, Any] = json.loads(line)
                        ts_str: str = (
                            record.get("timestamp", "")
                            if isinstance(record, dict)
                            else ""
                        )
                        # Handle both formats: with and without Z suffix
                        # (empty/missing timestamp raises ValueError -> skipped)
                        ts_str = ts_str.replace("Z", "+00:00")
                        ts = datetime.fromisoformat(ts_str).timestamp()
                    except (json.JSONDecodeError, ValueError):
                        skipped += 1
                        continue
                    if first_ts is None:
                        first_ts = ts
                    if ts >= cutoff:
                        records.append(record)

                covered = offset == 0 or (first_ts is not None and first_ts < cutoff)
                # offset > 0 with a maxed window means we can't grow further.
                hit_cap = offset > 0 and capped >= max_bytes
                if covered or hit_cap:
                    truncated = hit_cap and not covered
                    if skipped:
                        logger.warning(
                            "History read: skipped %d malformed lines", skipped
                        )
                    if truncated:
                        logger.warning(
                            "History read: hit %d-byte cap before covering %.1fh; "
                            "returning most recent %d records",
                            max_bytes,
                            hours,
                            len(records),
                        )
                    return records, skipped, truncated
                read_size *= 2
    except Exception as e:
        logger.error("Failed to read history file: %s", e)
        return str(e)


def _recording_health() -> dict[str, Any]:
    """Recorder liveness for the frontend: last success, last error, and whether
    it looks stalled — no successful record within ~2 record intervals plus one
    sample window (comfortably longer than a normal gap, so it doesn't false-fire
    every cycle). This is what turns a silently-dead recorder into a visible one.
    """
    last_success = getattr(APP.state, "history_last_success", None)
    interval = getattr(
        APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL
    )
    duration = getattr(
        APP.state, "history_sample_duration", DEFAULT_HISTORY_SAMPLE_DURATION
    )
    stale = False
    # No success since start: age from the start time, so a recorder that has
    # never worked (Glances dead from boot) still trips stale on schedule.
    since = last_success or getattr(APP.state, "started_at", None)
    if since:
        try:
            age = (
                datetime.now(timezone.utc).timestamp()
                - datetime.fromisoformat(
                    str(since).replace("Z", "+00:00")
                ).timestamp()
            )
            stale = age > 2 * interval + duration
        except ValueError:
            pass
    return {
        "last_success": last_success,
        "last_error": getattr(APP.state, "history_last_error", None),
        "interval_sec": interval,
        "stale": stale,
    }


@APP.get("/api/history")
async def get_history(hours: float = 24) -> Response:
    """Return historical data for the specified time range.

    Args:
        hours: Number of hours of history to return (default 24, max 8760).
    """
    hours = max(0.1, min(hours, 8760))
    result = await asyncio.to_thread(_read_history, hours)

    if isinstance(result, str):
        return Response(
            content=json.dumps({"error": "Failed to read history", "detail": result}),
            status_code=500,
            media_type="application/json",
        )

    records, skipped, truncated = result
    # Records embed an `ip` block (LAN/public/Tailscale). Nothing in the UI reads it
    # from history — the IP-Info card uses live /api/ip-info — so don't serve a
    # network-topology history to unauthenticated LAN clients. The password-gated
    # /api/history/raw still exposes it for deliberate export. Records are freshly
    # parsed per request, so popping in place touches nothing shared.
    # The heavy per-record fields are served by /api/history/field/<name> on demand.
    for rec in records:
        rec.pop("ip", None)
        for name in HISTORY_SIDE_FIELDS:
            rec.pop(name, None)
    return Response(
        content=json.dumps(
            {
                "records": records,
                "count": len(records),
                "skipped_lines": skipped,
                "truncated": truncated,
                "recording": _recording_health(),
            }
        ),
        media_type="application/json",
    )


@APP.get("/api/history/field/{name}")
async def get_history_field(name: str, hours: float = 24) -> Response:
    """One heavy record field over the range: ``[{timestamp, <name>}, ...]``.

    Only the names in HISTORY_SIDE_FIELDS (the ones /api/history leaves out) are
    served; a record without the field keeps its timestamp so the chart's x-axis
    matches /api/history and the missing field draws as a gap.
    """
    if name not in HISTORY_SIDE_FIELDS:
        return Response(
            content=json.dumps({"error": f"unknown field; one of {sorted(HISTORY_SIDE_FIELDS)}"}),
            status_code=404,
            media_type="application/json",
        )
    hours = max(0.1, min(hours, 8760))
    result = await asyncio.to_thread(_read_history, hours)
    if isinstance(result, str):
        return Response(
            content=json.dumps({"error": "Failed to read history", "detail": result}),
            status_code=500,
            media_type="application/json",
        )
    records, skipped, truncated = result
    slim = [
        {"timestamp": rec.get("timestamp"), **({name: rec[name]} if name in rec else {})}
        for rec in records
    ]
    return Response(
        content=json.dumps(
            {
                "field": name,
                "records": slim,
                "count": len(slim),
                "skipped_lines": skipped,
                "truncated": truncated,
            }
        ),
        media_type="application/json",
    )


@APP.get("/api/history/raw")
async def get_history_raw(request: Request) -> Response:
    """Stream the raw history JSONL file (full export, no parsing).

    Password-gated: every record carries an ``ip`` block (LAN, public v4/v6 and
    Tailscale addresses), so this is a one-request export of the host's network
    topology over time. The dashboard UI never calls it — it exists for manual
    export — so the gate costs nothing in the browser. Pass the password as
    ``?password=`` or an ``X-Dashboard-Password`` header.
    """
    if not PASSWORD_FILE.exists():
        return Response(
            content=json.dumps({"error": "No password configured on server"}),
            status_code=503,
            media_type="application/json",
        )
    password: str = request.query_params.get("password") or request.headers.get(
        "X-Dashboard-Password", ""
    )
    denied = await _verify_password_throttled(request, password)
    if denied is not None:
        return denied

    if not HISTORY_FILE.exists():
        return Response(
            content=json.dumps({"error": "No history file"}),
            status_code=404,
            media_type="application/json",
        )
    return FileResponse(
        HISTORY_FILE,
        media_type="application/x-ndjson",
        filename="history.jsonl",
    )


@APP.post("/api/history/record")
async def trigger_history_record() -> Response:
    """Manually trigger a history recording (rate-limited to 1/minute)."""
    global _last_manual_record_time

    now: float = time.time()
    elapsed: float = now - _last_manual_record_time
    if elapsed < APP.state.manual_record_min_interval:
        return Response(
            content=json.dumps(
                {
                    "status": "skipped",
                    "reason": "rate_limited",
                    "retry_after": int(APP.state.manual_record_min_interval - elapsed),
                }
            ),
            media_type="application/json",
            status_code=429,
        )

    try:
        _last_manual_record_time = now
        glances_url = APP.state.glances_url
        record = await collect_history_sample(glances_url)
        await asyncio.to_thread(append_history_record, record)
        _apply_record_health(record)
        # Echo without `ip` (see get_history). Build a new dict rather than popping:
        # `record` is the object that was just written and must stay intact.
        echoed = {key: value for key, value in record.items() if key != "ip"}
        return Response(
            content=json.dumps({"status": "ok", "record": echoed}),
            media_type="application/json",
        )
    except Exception as e:
        logger.error("Manual history recording failed: %s", e)
        return Response(
            content=json.dumps({"error": "Recording failed", "detail": str(e)}),
            status_code=500,
            media_type="application/json",
        )


@APP.get("/api/history/settings")
async def get_history_settings() -> Response:
    """Return current history recording settings."""
    return Response(
        content=json.dumps(
            {
                "record_interval": APP.state.history_record_interval,
                "sample_duration": APP.state.history_sample_duration,
                "sample_count": APP.state.history_sample_count,
                "min_record_interval": APP.state.manual_record_min_interval,
                "ping_enabled": APP.state.ping_enabled,
                "processes_enabled": APP.state.history_processes_enabled,
                "process_count": APP.state.history_process_count,
                "defaults": {
                    "record_interval": DEFAULT_HISTORY_RECORD_INTERVAL,
                    "sample_duration": DEFAULT_HISTORY_SAMPLE_DURATION,
                    "sample_count": DEFAULT_HISTORY_SAMPLE_COUNT,
                    "min_record_interval": DEFAULT_MANUAL_RECORD_MIN_INTERVAL,
                    "ping_enabled": True,
                    "processes_enabled": DEFAULT_HISTORY_PROCESSES_ENABLED,
                    "process_count": DEFAULT_HISTORY_PROCESS_COUNT,
                },
                "password_configured": PASSWORD_FILE.exists(),
            }
        ),
        media_type="application/json",
    )


@APP.post("/api/history/settings")
async def update_history_settings(request: Request) -> Response:
    """Update history recording settings (requires password)."""
    if not PASSWORD_FILE.exists():
        return Response(
            content=json.dumps({"error": "No password configured on server"}),
            status_code=503,
            media_type="application/json",
        )

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = None  # type: ignore[assignment]
    if not isinstance(body, dict):
        return Response(
            content=json.dumps({"error": "Invalid JSON: expected an object"}),
            status_code=400,
            media_type="application/json",
        )

    password: str = body.get("password", "")
    denied = await _verify_password_throttled(request, password)
    if denied is not None:
        return denied

    # Validate new values
    record_interval = body.get("record_interval")
    sample_duration = body.get("sample_duration")
    sample_count = body.get("sample_count")
    min_record_interval = body.get("min_record_interval")
    process_count = body.get("process_count")
    errors: list[str] = []

    if record_interval is not None:
        try:
            record_interval = int(record_interval)
            if not (10 <= record_interval <= 86400):
                errors.append("record_interval must be between 10 and 86400 seconds")
        except (ValueError, TypeError):
            errors.append("record_interval must be an integer")

    if sample_duration is not None:
        try:
            sample_duration = int(sample_duration)
            if not (1 <= sample_duration <= 300):
                errors.append("sample_duration must be between 1 and 300 seconds")
        except (ValueError, TypeError):
            errors.append("sample_duration must be an integer")

    if sample_count is not None:
        try:
            sample_count = int(sample_count)
            if not (1 <= sample_count <= 100):
                errors.append("sample_count must be between 1 and 100")
        except (ValueError, TypeError):
            errors.append("sample_count must be an integer")

    if min_record_interval is not None:
        try:
            min_record_interval = int(min_record_interval)
            if not (0 <= min_record_interval <= 3600):
                errors.append("min_record_interval must be between 0 and 3600 seconds")
        except (ValueError, TypeError):
            errors.append("min_record_interval must be an integer")

    if process_count is not None:
        try:
            process_count = int(process_count)
            if not (1 <= process_count <= 50):
                errors.append("process_count must be between 1 and 50")
        except (ValueError, TypeError):
            errors.append("process_count must be an integer")

    if errors:
        return Response(
            content=json.dumps({"error": "Validation failed", "details": errors}),
            status_code=422,
            media_type="application/json",
        )

    # Apply changes
    changed = False
    if (
        record_interval is not None
        and record_interval != APP.state.history_record_interval
    ):
        APP.state.history_record_interval = record_interval
        changed = True
    if (
        sample_duration is not None
        and sample_duration != APP.state.history_sample_duration
    ):
        APP.state.history_sample_duration = sample_duration
        changed = True
    if (
        sample_count is not None
        and sample_count != APP.state.history_sample_count
    ):
        APP.state.history_sample_count = sample_count
        changed = True
    if (
        min_record_interval is not None
        and min_record_interval != APP.state.manual_record_min_interval
    ):
        APP.state.manual_record_min_interval = min_record_interval
        changed = True

    if (
        process_count is not None
        and process_count != APP.state.history_process_count
    ):
        APP.state.history_process_count = process_count
        changed = True

    ping_enabled = body.get("ping_enabled")
    if ping_enabled is not None and APP.state.ping_enabled != (ping_enabled is True):
        APP.state.ping_enabled = ping_enabled is True
        changed = True

    processes_enabled = body.get("processes_enabled")
    if processes_enabled is not None and APP.state.history_processes_enabled != (
        processes_enabled is True
    ):
        APP.state.history_processes_enabled = processes_enabled is True
        changed = True

    # Restart history recorder task if interval/duration changed
    if changed and APP.state.history_task is not None:
        APP.state.history_task.cancel()
        try:
            await APP.state.history_task
        except asyncio.CancelledError:
            pass
        APP.state.history_task = asyncio.create_task(history_recorder())
    if changed:
        logger.info(
            "History settings updated: interval=%ds, duration=%ds, count=%d, min_record=%ds, ping=%s, processes=%s (top %d)",
            APP.state.history_record_interval,
            APP.state.history_sample_duration,
            APP.state.history_sample_count,
            APP.state.manual_record_min_interval,
            APP.state.ping_enabled,
            APP.state.history_processes_enabled,
            APP.state.history_process_count,
        )

    return Response(
        content=json.dumps(
            {
                "status": "ok",
                "record_interval": APP.state.history_record_interval,
                "sample_duration": APP.state.history_sample_duration,
                "sample_count": APP.state.history_sample_count,
                "min_record_interval": APP.state.manual_record_min_interval,
                "ping_enabled": APP.state.ping_enabled,
                "processes_enabled": APP.state.history_processes_enabled,
                "process_count": APP.state.history_process_count,
            }
        ),
        media_type="application/json",
    )


@APP.get("/api/ping")
async def ping() -> Response:
    """Ping all configured targets and return latencies."""
    if not APP.state.ping_enabled:
        return Response(
            content=json.dumps({"disabled": True}),
            media_type="application/json",
        )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(asyncio.to_thread(_run_ping, t) for t in PING_TARGETS)),
            timeout=8,
        )
    # asyncio.TimeoutError is a distinct class from builtin TimeoutError before
    # Python 3.11 (the declared floor is 3.10), so catch both.
    except (asyncio.TimeoutError, TimeoutError):
        results = [None] * len(PING_TARGETS)
    targets = {}
    for target, latency in zip(PING_TARGETS, results):
        if latency is not None:
            targets[target] = {"latency_ms": round(latency, 2)}
        else:
            targets[target] = {"error": "timeout"}
    return Response(
        content=json.dumps({"targets": targets}),
        media_type="application/json",
    )


def _get_gpu_info() -> list[dict[str, int | float | str | None]]:
    """Detailed GPU info from nvidia-smi (sync, called from thread pool).

    One shape: a list of GPU dicts, empty when nvidia-smi is missing, times out
    or fails (like get_gpu_stats); the reason goes to the log. The UI's cache
    readers and test.py rely on always getting a list.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        logger.debug("nvidia-smi not found")
        return []
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("nvidia-smi error: %s", e)
        return []
    if result.returncode != 0:
        logger.warning("nvidia-smi failed: %s", result.stderr.strip() or f"exit {result.returncode}")
        return []

    # Get CUDA version from nvidia-smi header
    cuda_version: str = ""
    try:
        cuda_result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in cuda_result.stdout.split("\n"):
            if "CUDA Version:" in line:
                # Extract version like "12.4" from "CUDA Version: 12.4"
                version_parts = line.split("CUDA Version:")[1].strip().split()
                cuda_version = version_parts[0] if version_parts else ""
                break
    except Exception:
        pass  # CUDA version is optional — GPU data still valid

    gpus: list[dict[str, int | float | str | None]] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) >= 9:
            try:
                # power.draw may be "[N/A]" if not supported
                try:
                    power_draw = float(parts[6])
                except (ValueError, TypeError):
                    power_draw = None
                try:
                    power_limit = float(parts[7])
                except (ValueError, TypeError):
                    power_limit = None
                gpus.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "utilization": float(parts[2]),  # %
                        "memory_used": float(parts[3]),  # MiB
                        "memory_total": float(parts[4]),  # MiB
                        "temperature": float(parts[5]),  # °C
                        "power_draw": power_draw,  # W
                        "power_limit": power_limit,  # W
                        "driver_version": parts[8],
                        "cuda_version": cuda_version,
                    }
                )
            except (ValueError, TypeError, IndexError):
                logger.warning("Skipping malformed nvidia-smi line: %s", line)
                continue

    return gpus


@APP.get("/api/gpu-info")
async def gpu_info() -> Response:
    """Detailed GPU info from nvidia-smi; [] when it is unavailable."""
    result = await asyncio.to_thread(_get_gpu_info)
    return Response(content=json.dumps(result), media_type="application/json")


def _get_cpu_info() -> dict[str, Any]:
    """Get detailed CPU info from /proc/cpuinfo (sync, called from thread pool)."""
    cpuinfo_path = Path("/proc/cpuinfo")
    if not cpuinfo_path.exists():
        return {}

    content = cpuinfo_path.read_text()

    model_name: str = ""
    physical_cores: set[str] = set()
    logical_cores: int = 0
    cpu_mhz: str = ""
    cache_size: str = ""

    for line in content.split("\n"):
        if ":" not in line:
            continue
        key: str
        value: str
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if key == "model name" and not model_name:
            model_name = value
        elif key == "core id":
            physical_cores.add(value)
        elif key == "processor":
            logical_cores += 1
        elif key == "cpu MHz" and not cpu_mhz:
            cpu_mhz = value
        elif key == "cache size" and not cache_size:
            cache_size = value

    # Try to get L3 cache from sysfs
    l3_cache: str = ""
    l3_path = Path("/sys/devices/system/cpu/cpu0/cache/index3/size")
    if l3_path.exists():
        l3_cache = l3_path.read_text().strip()

    return {
        "model": model_name,
        "physical_cores": len(physical_cores) if physical_cores else logical_cores,
        "logical_cores": logical_cores,
        "cache_size": cache_size,
        "l3_cache": l3_cache,
    }


@APP.get("/api/cpu-info")
async def cpu_info() -> Response:
    """Get detailed CPU info from /proc/cpuinfo."""
    try:
        result = await asyncio.to_thread(_get_cpu_info)
        return Response(content=json.dumps(result), media_type="application/json")
    except Exception as e:
        logger.warning("Failed to read CPU info: %s", e)
        return Response(
            content=json.dumps({"error": "Failed to read CPU info"}),
            status_code=500,
            media_type="application/json",
        )


def _get_ip_info() -> dict[str, Any]:
    """Collect IP addresses: LAN, public, and Tailscale (sync, thread pool)."""
    result: dict[str, Any] = {"lan": [], "public": {}, "tailscale": {}}

    # --- LAN IPs via `ip -j addr show` ---
    try:
        ip_out = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ip_out.returncode == 0:
            interfaces: list[dict[str, Any]] = json.loads(ip_out.stdout)
            for iface in interfaces:
                name: str = iface.get("ifname", "")
                if name == "lo":
                    continue
                ipv4_addrs: list[str] = []
                ipv6_addrs: list[str] = []
                for addr_info in iface.get("addr_info", []):
                    family: str = addr_info.get("family", "")
                    local: str = addr_info.get("local", "")
                    if not local:
                        continue
                    if family == "inet":
                        ipv4_addrs.append(local)
                    elif family == "inet6" and not local.startswith("fe80"):
                        ipv6_addrs.append(local)
                if ipv4_addrs or ipv6_addrs:
                    entry: dict[str, Any] = {"interface": name}
                    if ipv4_addrs:
                        entry["ipv4"] = (
                            ipv4_addrs if len(ipv4_addrs) > 1 else ipv4_addrs[0]
                        )
                    if ipv6_addrs:
                        entry["ipv6"] = (
                            ipv6_addrs if len(ipv6_addrs) > 1 else ipv6_addrs[0]
                        )
                    result["lan"].append(entry)
    except Exception as e:
        logger.warning("Failed to get LAN IPs: %s", e)

    # --- Public IP via external services ---
    try:
        with httpx.Client(timeout=3) as client:
            try:
                resp = client.get("https://api.ipify.org")
                if resp.status_code == 200 and resp.text.strip():
                    result["public"]["ipv4"] = resp.text.strip()
            except Exception as e:
                logger.debug("Failed to get public IPv4: %s", e)
            try:
                resp = client.get("https://api64.ipify.org")
                if resp.status_code == 200 and resp.text.strip():
                    addr = resp.text.strip()
                    # api64 returns IPv4 if no IPv6 — only store if different
                    if addr != result["public"].get("ipv4"):
                        result["public"]["ipv6"] = addr
            except Exception as e:
                logger.debug("Failed to get public IPv6: %s", e)
    except Exception as e:
        logger.warning("Failed to create HTTP client for public IP: %s", e)

    if not result["public"].get("ipv4") and not result["public"].get("ipv6"):
        result["public"]["error"] = "lookup failed"

    # --- Tailscale IP ---
    try:
        ts_out = subprocess.run(
            ["tailscale", "ip"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ts_out.returncode == 0:
            lines = [
                ln.strip() for ln in ts_out.stdout.strip().splitlines() if ln.strip()
            ]
            for line in lines:
                if ":" in line:
                    result["tailscale"]["ipv6"] = line
                else:
                    result["tailscale"]["ipv4"] = line
    except FileNotFoundError:
        logger.debug("tailscale not installed")
    except Exception as e:
        logger.warning("Failed to get Tailscale IPs: %s", e)

    return result


@APP.get("/api/ip-info")
async def ip_info() -> Response:
    """Get IP addresses: LAN, public, and Tailscale."""
    try:
        result = await asyncio.to_thread(_get_ip_info)
        return Response(content=json.dumps(result), media_type="application/json")
    except Exception as e:
        logger.warning("Failed to get IP info: %s", e)
        return Response(
            content=json.dumps({"error": "Failed to get IP info"}),
            status_code=500,
            media_type="application/json",
        )


def _get_gpu_processes() -> list[dict[str, Any]]:
    """Get processes using GPU from nvidia-smi (sync, called from thread pool).

    # TODO: surface per-line parse warnings to the frontend

    Returns a list of process dicts on success, or an error dict on failure.
    """
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return []

    # Get GPU index mapping from UUID
    uuid_result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    uuid_to_index: dict[str, int] = {}
    for line in uuid_result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                uuid_to_index[parts[1]] = int(parts[0])
            except ValueError:
                logger.warning("Skipping GPU UUID line with bad index: %s", line)

    processes: list[dict[str, Any]] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpu_uuid: str = parts[0]
                pid: int = int(parts[1])
                nvidia_name: str = parts[2]
                # Get sanitized process info from /proc
                info = get_sanitized_process_info(
                    pid, Path(nvidia_name).name, nvidia_name
                )
                processes.append(
                    {
                        "gpu_index": uuid_to_index.get(gpu_uuid, 0),
                        "pid": pid,
                        "name": info["name"],
                        "path": info["path"],
                        "gpu_memory_mib": float(parts[3]),
                    }
                )
            except (ValueError, IndexError) as e:
                logger.warning("Skipping malformed GPU process line: %s (%s)", line, e)

    return processes


@APP.get("/api/gpu-processes")
async def gpu_processes() -> Response:
    """Get processes using GPU from nvidia-smi."""
    try:
        result = await asyncio.to_thread(_get_gpu_processes)
    except FileNotFoundError:
        logger.debug("nvidia-smi not found for GPU processes")
        return Response(content="[]", media_type="application/json")
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi timed out for GPU processes")
        return Response(content="[]", media_type="application/json")

    return Response(content=json.dumps(result), media_type="application/json")


def _run_pidstat(duration: int, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run pidstat synchronously (called from thread pool)."""
    return subprocess.run(
        ["pidstat", "-d", str(duration), "1"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@APP.get("/api/disk-processes")
async def disk_processes(duration: int = 1) -> Response:
    """Get per-process disk I/O using pidstat.

    Args:
        duration: Sample duration in seconds (1-10). Default 1s.
    """
    # Non-blocking lock acquisition - return immediately if capture in progress
    if _disk_lock.locked():
        return Response(
            content=json.dumps(
                {"status": "in_progress", "detail": "Capture already running"}
            ),
            media_type="application/json",
        )
    await _disk_lock.acquire()

    try:
        # Clamp duration to reasonable range
        duration = max(1, min(duration, 10))

        # pidstat -d: disk I/O stats
        # Args: interval (seconds), count (number of samples)
        result = await asyncio.to_thread(
            _run_pidstat, duration=duration, timeout=duration + 5
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Exit code {result.returncode}"
            logger.warning("pidstat failed: %s", error_msg)
            return Response(
                content=json.dumps({"error": "pidstat failed", "detail": error_msg}),
                media_type="application/json",
            )

        # Parse pidstat output
        # Format: Average:  UID  PID  kB_rd/s  kB_wr/s  kB_ccwr/s  iodelay  Command
        # Fields:    [0]    [1]  [2]    [3]      [4]       [5]       [6]      [7]
        processes: dict[int, dict[str, Any]] = {}

        for line in result.stdout.strip().split("\n"):
            line: str = line.strip()
            if not line:
                continue

            # Look for "Average:" section (most accurate values)
            if not line.startswith("Average:"):
                continue

            # Skip the column header line
            if "PID" in line and "kB_rd/s" in line:
                continue

            fields: list[str] = line.split()
            if len(fields) < 8:
                continue

            try:
                # Average: UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command
                pid: int = int(fields[2])
                read_kbps: float = float(fields[3])
                write_kbps: float = float(fields[4])

                # Only include processes with actual I/O
                if read_kbps > 0 or write_kbps > 0:
                    command: str = fields[7] if len(fields) > 7 else "unknown"
                    info = get_sanitized_process_info(pid, command, "")
                    processes[pid] = {
                        "pid": pid,
                        "read_kbps": read_kbps,
                        "write_kbps": write_kbps,
                        "name": info["name"],
                        "path": info["path"],
                    }
            except (ValueError, IndexError):
                continue

        return Response(
            content=json.dumps(list(processes.values())),
            media_type="application/json",
        )

    except FileNotFoundError:
        logger.debug("pidstat not found")
        return Response(
            content=json.dumps(
                {
                    "error": "pidstat not found",
                    "detail": "Install with: apt install sysstat",
                }
            ),
            media_type="application/json",
        )
    except subprocess.TimeoutExpired:
        logger.warning("pidstat timed out")
        return Response(
            content=json.dumps(
                {
                    "error": "pidstat timeout",
                    "detail": f"Command took longer than {duration + 5}s",
                }
            ),
            media_type="application/json",
        )
    except Exception as e:
        logger.warning("pidstat error: %s", e)
        return Response(
            content=json.dumps(
                {"error": "Disk processes unavailable", "detail": str(e)}
            ),
            media_type="application/json",
        )
    finally:
        _disk_lock.release()


def _run_nethogs(cycles: int, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run nethogs synchronously (called from thread pool)."""
    return subprocess.run(
        ["nethogs", "-t", "-c", str(cycles), "-d", "1"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@APP.get("/api/network-processes")
async def network_processes(duration: int = 3) -> Response:
    """Get per-process network bandwidth using nethogs."""
    # Non-blocking lock acquisition - return immediately if capture in progress
    if _nethogs_lock.locked():
        return Response(
            content=json.dumps(
                {"status": "in_progress", "detail": "Capture already running"}
            ),
            media_type="application/json",
        )
    await _nethogs_lock.acquire()

    try:
        # nethogs requires CAP_NET_ADMIN or root
        # -t: trace mode (stdout instead of ncurses)
        # -c N: run N cycles then exit
        # -d 1: 1 second delay between refreshes
        cycles: int = max(1, min(duration, 60))
        timeout: int = int(cycles * 1.5) + 5
        result = await asyncio.to_thread(_run_nethogs, cycles=cycles, timeout=timeout)

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Exit code {result.returncode}"
            # Log full error to server console
            logger.warning("nethogs failed: %s", error_msg)
            if (
                "permission" in error_msg.lower()
                or "operation not permitted" in error_msg.lower()
                or "capabilities" in error_msg.lower()
            ):
                return Response(
                    content=json.dumps(
                        {
                            "error": "nethogs requires capabilities",
                            "detail": "Run: sudo setcap cap_net_admin,cap_net_raw,cap_dac_read_search,cap_sys_ptrace+ep $(which nethogs)",
                        }
                    ),
                    media_type="application/json",
                )
            return Response(
                content=json.dumps({"error": "nethogs failed", "detail": error_msg}),
                media_type="application/json",
            )

        # Parse nethogs trace mode output
        # Format: <path>/<pid>/<uid>\t<sent_kb/s>\t<recv_kb/s>
        processes: dict[int, dict[str, Any]] = {}

        for line in result.stdout.strip().split("\n"):
            line: str = line.strip()
            if not line or line.startswith("Refreshing"):
                continue

            # nethogs -t is tab-separated: program path, sent, received. Splitting
            # on any whitespace would break a program path containing a space.
            fields: list[str] = [f.strip() for f in line.split("\t")]
            if len(fields) < 3:
                continue

            try:
                recv_kbps: float = float(fields[-1])
                sent_kbps: float = float(fields[-2])
                proc_info: str = fields[0]  # e.g., "/usr/bin/firefox/12345/1000"

                # Extract PID from proc_info (format: path/pid/uid)
                parts: list[str] = proc_info.rsplit("/", 2)
                if len(parts) >= 3:
                    pid = int(parts[-2])
                    program = parts[0].split("/")[-1] if "/" in parts[0] else parts[0]
                else:
                    continue

                # Aggregate (nethogs outputs multiple lines per process across cycles)
                if pid in processes:
                    processes[pid]["sent_kbps"] = max(
                        processes[pid]["sent_kbps"], sent_kbps
                    )
                    processes[pid]["recv_kbps"] = max(
                        processes[pid]["recv_kbps"], recv_kbps
                    )
                else:
                    # Get sanitized process info from /proc
                    info = get_sanitized_process_info(pid, program, parts[0])
                    processes[pid] = {
                        "pid": pid,
                        "name": info["name"],
                        "path": info["path"],
                        "sent_kbps": sent_kbps,
                        "recv_kbps": recv_kbps,
                    }
            except (ValueError, IndexError):
                continue

        # Filter out zero-bandwidth processes
        active: list[dict[str, Any]] = [
            p for p in processes.values() if p["sent_kbps"] > 0 or p["recv_kbps"] > 0
        ]
        return Response(content=json.dumps(active), media_type="application/json")

    except FileNotFoundError:
        logger.debug("nethogs not found")
        return Response(
            content=json.dumps(
                {
                    "error": "nethogs not found",
                    "detail": "Install with: apt install nethogs",
                }
            ),
            media_type="application/json",
        )
    except subprocess.TimeoutExpired:
        logger.warning("nethogs timed out")
        return Response(
            content=json.dumps(
                {
                    "error": "nethogs timeout",
                    "detail": f"Command took longer than {timeout}s",
                }
            ),
            media_type="application/json",
        )
    except Exception as e:
        logger.warning("nethogs error: %s", e)
        return Response(
            content=json.dumps(
                {"error": "Network processes unavailable", "detail": str(e)}
            ),
            media_type="application/json",
        )
    finally:
        _nethogs_lock.release()


@APP.get("/api/processlist")
async def processlist() -> Response:
    """Get process list from Glances with sanitized display info."""
    glances_url: str = APP.state.glances_url
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{glances_url}/api/4/processlist", timeout=10.0)
            if resp.status_code != 200:
                return Response(content=resp.content, status_code=resp.status_code)

            processes: list[dict[str, Any]] = resp.json()
            for proc in processes:
                pid: Any = proc.get("pid")
                if pid:
                    name: str = proc.get("name", "unknown")
                    info = get_sanitized_process_info(pid, name)
                    proc["display_name"] = info["name"]
                    proc["display_path"] = info["path"]
                # Remove cmdline to prevent leaking sensitive data
                proc.pop("cmdline", None)

            return Response(
                content=json.dumps(processes), media_type="application/json"
            )
        except httpx.RequestError as error:
            logger.error("Glances API unavailable: %s", error)
            return Response(
                content=json.dumps(
                    {"error": "Glances API unavailable", "detail": str(error)}
                ),
                status_code=502,
                media_type="application/json",
            )


@APP.get("/api/4/fs")
async def proxy_fs(request: Request) -> Response:
    """Proxy /api/4/fs with device symlinks resolved for diskio matching, and
    duplicate-device entries de-duped (e.g. PrivateTmp's /tmp, /var/tmp bind
    mounts that mirror /) so each physical filesystem shows once."""
    glances_url: str = request.app.state.glances_url
    async with httpx.AsyncClient() as client:
        try:
            resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/fs", timeout=10.0
            )
            if resp.status_code == 200:
                data: list = resp.json()
                for entry in data:
                    device_name: str = entry.get("device_name", "")
                    if device_name:
                        entry["device_name"] = os.path.realpath(device_name)
                data = _dedupe_fs_by_device(data)
                return Response(content=json.dumps(data), media_type="application/json")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )
        except httpx.RequestError as error:
            logger.error("Glances fs API unavailable: %s", error)
            return Response(
                content=json.dumps([]),
                status_code=502,
                media_type="application/json",
            )


# ---------------------------------------------------------------------------
# System mail: smartd / mdadm / cron notifications delivered by a local-only MTA
# to the service user's mbox spool (root aliased to that user in /etc/aliases).
# Only the count and the newest subjects are served; the full text is read with
# mail(1) on the box.
# ---------------------------------------------------------------------------

# systemd's User= sets $USER, and getpass falls back to the password database.
MAIL_SPOOL: Final[str] = f"/var/mail/{getpass.getuser()}"
MAIL_LIMIT: Final[int] = 10


def _get_mail(path: str = MAIL_SPOOL, limit: int = MAIL_LIMIT) -> dict[str, Any]:
    """Message count and the newest ``limit`` subjects of an mbox spool (sync). Never raises."""
    result: dict[str, Any] = {
        "status": "ok",
        "detail": None,
        "path": path,
        "count": 0,
        "unreadable": 0,
        "messages": [],
    }
    try:
        # create=False: a mistyped path must not create an empty spool. The box is
        # never flushed, so it never writes.
        box = mailbox.mbox(path, create=False)
        try:
            keys = box.keys()
            result["count"] = len(keys)
            for key in reversed(keys[-limit:]):
                try:
                    msg = email.message_from_bytes(
                        box.get_bytes(key), policy=email.policy.default
                    )
                    # With policy.default the header is a DateHeader whose .datetime
                    # is None on garbage; the header itself is None when absent.
                    d = msg["Date"]
                    entry: dict[str, Any] = {
                        "date_epoch": (
                            d.datetime.timestamp()
                            if d is not None and d.datetime is not None
                            else None
                        ),
                        "subject": str(msg.get("Subject", "")) or "(no subject)",
                    }
                except Exception as e:
                    # Never skipped: the row itself says this message could not be
                    # parsed, the readable ones around it still show, and the count
                    # of these turns the whole card red (renderMail) -- loud, nothing hidden.
                    result["unreadable"] += 1
                    entry = {
                        "date_epoch": None,
                        "subject": f"unreadable message ({type(e).__name__})",
                        "error": type(e).__name__,
                    }
                result["messages"].append(entry)
        finally:
            box.close()
    except mailbox.NoSuchMailboxError:
        result["status"] = "missing"
    except Exception as e:
        # One bad message or an unreadable spool must not take the card down; say why.
        result["status"] = "error"
        result["detail"] = type(e).__name__
    return result


@APP.get("/api/mail")
async def mail() -> Response:
    """Local system mail: count and newest subjects. Always 200; ``status`` says why not."""
    result = await asyncio.to_thread(_get_mail)
    return Response(content=json.dumps(result), media_type="application/json")


# ---------------------------------------------------------------------------
# Hardware health: the root collector (collector/health-collect.sh, every
# 15 min on a systemd timer) writes one JSON file with the alarm
# rules already applied (``alarms``). The dashboard serves it as-is plus a
# freshness status, so the web process never needs smartctl, ipmitool or sudo.
# ---------------------------------------------------------------------------

STATE_PATH: Final[str] = "/var/lib/horatio/state.json"
STATE_STALE_SEC: Final[int] = 60 * 60  # four missed 15-min runs
# The 60 s sensors snapshot (fans, chassis temps, BMC + RAPL power) written by
# health-sensors.sh next to state.json; the full collector alarms past 300 s too.
SENSORS_PATH: Final[str] = "/var/lib/horatio/sensors.json"
SENSORS_STALE_SEC: Final[int] = 300


def _get_storage_health(
    path: str = STATE_PATH, stale_sec: int = STATE_STALE_SEC
) -> dict[str, Any]:
    """A collector snapshot plus ``status``/``age_sec`` (sync). Never raises."""
    result: dict[str, Any] = {"status": "ok", "detail": None, "age_sec": None, "path": path}
    try:
        with open(path, "rb") as f:
            state = json.load(f)
    except FileNotFoundError:
        result["status"] = "missing"
        return result
    except (OSError, ValueError) as e:
        result["status"] = "error"
        result["detail"] = type(e).__name__
        return result
    if not isinstance(state, dict):
        result["status"] = "error"
        result["detail"] = "not a JSON object"
        return result
    ts = state.get("ts")
    # json.load accepts NaN/Infinity; int() of those raises, so they are an error.
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
        result["age_sec"] = int(time.time() - ts)
        if result["age_sec"] > stale_sec:
            result["status"] = "stale"
    else:
        # No ts (or a non-number) can never be "ok": nothing would ever go stale.
        result["status"] = "error"
        result["detail"] = "ts missing" if ts is None else "ts is not a finite number"
    # The collector's own keys first, so status/age/path can never be overridden.
    return {**state, **result}


@APP.get("/api/storage-health")
async def storage_health() -> Response:
    """The collector's hardware health snapshot with alarms. Always 200; ``status`` says freshness."""
    result = await asyncio.to_thread(_get_storage_health)
    return Response(content=json.dumps(result), media_type="application/json")


@APP.get("/api/sensors")
async def sensors_snapshot() -> Response:
    """The 60 s fans / temps / power snapshot. Always 200; ``status`` says freshness."""
    result = await asyncio.to_thread(_get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC)
    return Response(content=json.dumps(result), media_type="application/json")


@APP.get("/api/list-endpoints")
async def list_endpoints() -> Response:
    """Return list of all API endpoints this server implements."""
    endpoints = [
        "/4/cpu", "/4/percpu", "/4/mem", "/4/memswap", "/4/gpu",
        "/4/network", "/4/diskio", "/4/sensors", "/4/load", "/4/uptime",
        "/4/system", "/4/fs",
        "/cpu-info", "/gpu-info", "/ip-info", "/ping",
        "/gpu-processes", "/disk-processes", "/network-processes",
        "/processlist",
        "/history", "/history/settings", "/history/record",
        "/history/field/temps", "/history/field/processes",
        "/mail", "/storage-health", "/sensors",
        "/list-endpoints",
    ]
    return Response(
        content=json.dumps({"endpoints": endpoints, "backend": "python"}),
        media_type="application/json",
    )


@APP.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(path: str, request: Request) -> Response:
    """Proxy all /api/* requests to Glances."""
    # Glances' raw process views carry full cmdlines (/4/all, /4/processlist,
    # /4/processlist/top/N, /4/processes/<pid>, ...); /api/processlist is the
    # sanitized route and the only one the UI calls.
    # The deny check sees the path with "." and ".." collapsed, which is what
    # Glances will see ("4/x/../processlist"); the request itself forwards the
    # path as sent, so "//" and a trailing slash reach Glances untouched.
    resolved = posixpath.normpath("/" + path)[1:]
    if resolved == "4/all" or resolved.startswith(("4/processlist", "4/processes", "4/programlist")):
        return Response(status_code=404)
    glances_url: str = request.app.state.glances_url
    async with httpx.AsyncClient() as client:
        url: str = f"{glances_url}/api/{path}"
        try:
            resp: httpx.Response = await client.request(
                method=request.method,
                url=url,
                params=dict(request.query_params),
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in PROXY_STRIPPED_REQUEST_HEADERS
                },
                content=await request.body(),
                timeout=10.0,
            )
            response_headers: dict[str, str] = {
                key: value
                for key, value in resp.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=response_headers,
                media_type=resp.headers.get("content-type"),
            )
        except httpx.RequestError as error:
            logger.error("Glances API unavailable: %s", error)
            return Response(
                content=json.dumps(
                    {"error": "Glances API unavailable", "detail": str(error)}
                ),
                status_code=502,
                media_type="application/json",
            )


# ── Console text view (/text) ────────────────────────────────────────────────
# A curl-able rendering of the history file: the latest record as a snapshot
# and/or a downsampled table of recent records. "ascii" is plain 7-bit text;
# "unicode" adds block bars and sparklines.

TEXT_BAR_WIDTH: Final[int] = 20
# Default report width. The server can't see the client's terminal, so callers
# pass ?width=$COLUMNS; at TEXT_TWO_COLUMN_WIDTH or wider the default view puts
# the snapshot and the history side by side.
TEXT_WIDTH: Final[int] = 80
TEXT_MIN_WIDTH: Final[int] = 60
TEXT_TWO_COLUMN_WIDTH: Final[int] = 160
_SPARK_CHARS: Final[str] = "▁▂▃▄▅▆▇█"
_SPARK_ASCII: Final[str] = "_-#"
# The frontend's metric colours (static/style.css variables, as mapped in
# static/config.js), drawn with 24-bit ANSI escapes when colour is on.
TEXT_COLORS: Final[dict[str, str]] = {
    "cpu": "#3b82f6",  # --color-accent
    "cpu_cores": "#14b8a6",  # --color-cpu-cores
    "memory": "#059669",  # darker than the dashboard's --color-success, for contrast with ok
    "swap": "#a855f7",  # --color-swap
    "gpu": "#8b5cf6",  # --color-gpu
    "drive": "#84cc16",  # --color-read (drive usage card)
    "load": "#f59e0b",  # --color-warning (1-minute load)
    "net_in": "#06b6d4",  # --color-download
    "net_out": "#f97316",  # --color-upload
    "read": "#84cc16",  # --color-read
    "write": "#ef4444",  # --color-write
    "temp": "#f59e0b",  # --color-warning
    "ping": "#f59e0b",  # --color-ping-0
    "error": "#ef4444",  # --color-error
    "warning": "#f59e0b",  # --color-warning (the orange-card threshold)
    "ok": "#10b981",  # --color-success
    "temp_mid": "#eab308",  # --color-temp-mid
    "gpu_mem": "#ec4899",  # --color-gpu-mem
    "load_5m": "#eab308",  # --color-load-5m
    "load_15m": "#ca8a04",  # --color-load-15m
    "ping_0": "#f59e0b",  # --color-ping-0
    "ping_1": "#8b5cf6",  # --color-ping-1
    "ping_2": "#06b6d4",  # --color-ping-2
    "title": "#f0f0f0",  # --text-primary
    "muted": "#808080",
    "stat_min": "#6b7280",  # min / mean / max columns, darker for the smaller
    "stat_mean": "#a3a3a3",
}


def _ansi_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 colour: the grey ramp for near-greys, else the 6x6x6 cube."""
    if max(r, g, b) - min(r, g, b) < 16:
        return 232 + min(23, round((r + g + b) / 3 / 255 * 23))
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


_ANSI_16: Final[list[tuple[int, int, int]]] = [
    (0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
    (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
]


def _ansi_16(r: int, g: int, b: int) -> int:
    """Nearest of the 8 base colours (30-37), bright (90-97) when it's light."""
    i = min(range(8), key=lambda k: sum((a - c) ** 2 for a, c in zip((r, g, b), _ANSI_16[k])))
    return (90 if max(r, g, b) > 191 else 30) + i


def _paint(text: str, key: str, on: bool | str) -> str:
    """Colour `text` with TEXT_COLORS[key]. `on` is the colour depth: False for
    none, True or "24" for 24-bit, "256" or "16" for older terminals."""
    if not on or not text:
        return text
    h = TEXT_COLORS[key].lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    bold = "1;" if key == "title" else ""
    if on == "256":
        return f"\033[{bold}38;5;{_ansi_256(r, g, b)}m{text}\033[0m"
    if on == "16":
        return f"\033[{bold}{_ansi_16(r, g, b)}m{text}\033[0m"
    return f"\033[{bold}38;2;{r};{g};{b}m{text}\033[0m"


def _fmt(value: Any, spec: str = ".1f", none: str = "-") -> str:
    v = _num(value)
    return none if v is None else format(v, spec)


def _bar(
    percent: Any, unicode: bool, key: str = "cpu", color: bool = False,
    width: int = TEXT_BAR_WIDTH,
) -> str:
    p = _num(percent)
    filled = 0 if p is None else round(max(0.0, min(p, 100.0)) / 100 * width)
    if unicode:
        return _paint("█" * filled, key, color) + _paint("░" * (width - filled), "muted", color)
    return "[" + _paint("#" * filled, key, color) + " " * (width - filled) + "]"


def _range_bar(
    lo: Any, mid: Any, hi: Any, unicode: bool, key: str, color: bool,
    width: int = TEXT_BAR_WIDTH,
) -> str:
    """A 0-100 track with min..max filled and the median marked."""
    vals = [_num(lo), _num(mid), _num(hi)]
    if any(v is None for v in vals):
        return _bar(None, unicode, key, color, width)
    a, m, b = (round(max(0.0, min(v, 100.0)) / 100 * (width - 1)) for v in vals)
    track, span, mark = ("░", "▬", "◆") if unicode else (" ", "-", "|")
    cells = []
    for i in range(width):
        if i == m:
            cells.append(_paint(mark, key, color))
        elif a <= i <= b:
            cells.append(_paint(span, key, color))
        else:
            cells.append(_paint(track, "muted", color))
    body = "".join(cells)
    return body if unicode else f"[{body}]"


def _sparkline(
    values: list[float | None],
    chars: str = _SPARK_CHARS,
    lo: float | None = None,
    hi: float | None = None,
) -> str:
    """One character per value on the scale lo..hi (default: the data's own
    min..max); values outside it are clamped."""
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo = min(nums) if lo is None else lo
    hi = max(nums) if hi is None else hi
    span = (hi - lo) or 1.0
    top = len(chars) - 1
    return "".join(
        " " if v is None else chars[round(max(0.0, min(1.0, (v - lo) / span)) * top)]
        for v in values
    )


class _HistRow(NamedTuple):
    """One history metric: its sparkline row, table column and stats."""

    slot: str  # the _render_latest line it sits beside in two-column mode
    name: str
    values: list[float | None]  # per record (the record's average)
    spec: str  # number format
    key: str  # TEXT_COLORS key
    threshold: Any  # orange-card threshold, or None
    scale: str  # "pct" 0-100, "zero" 0..peak, "load" 0..max(cores, peak), "temp"
    lows: list[float | None] | None = None  # per-record burst minimum, when kept
    peaks: list[float | None] | None = None  # per-record burst maximum, when kept

    def bounds(self) -> tuple[float, float]:
        """The sparkline scale: fixed for percentages, from 0 for rates, so a
        72-74% memory wobble doesn't fill the whole height."""
        # Scale to the averages actually drawn, not burst peaks: one 60 MB/s
        # spike would otherwise flatten every other column.
        top = max([v for v in self.values if v is not None], default=0.0)
        if self.scale == "pct":
            return 0.0, 100.0
        if self.scale == "load":
            return 0.0, max(float(os.cpu_count() or 1), top)
        if self.scale == "temp":
            thr = _num(self.threshold) or 60.0
            return 20.0, max(thr + 10, top)
        return 0.0, max(top, 1.0)


def _time_buckets(
    times: list[float | None],
    values: list[float | None],
    start: float,
    end: float,
    width: int,
    hold: float,
) -> list[float | None]:
    """Values on a real time axis: `width` equal slices of [start, end], each the
    mean of the records inside it. An empty slice repeats the last earlier record
    when that record is at most `hold` seconds old (records are sparser than
    columns), and stays blank across real gaps (box off, recorder down)."""
    span = (end - start) / width or 1.0
    sums = [0.0] * width
    counts = [0] * width
    for t, v in zip(times, values):
        if t is None or v is None or not start <= t <= end:
            continue
        b = min(width - 1, int((t - start) / span))
        sums[b] += v
        counts[b] += 1
    out: list[float | None] = []
    last_v: float | None = None
    last_t = -math.inf
    for b in range(width):
        if counts[b]:
            last_v = sums[b] / counts[b]
            last_t = start + (b + 1) * span
            out.append(last_v)
        elif last_v is not None and start + b * span - last_t <= hold:
            out.append(last_v)
        else:
            out.append(None)
    return out


def _temps_of(record: dict[str, Any]) -> list[float]:
    return [v for v in (_num(t) for t in (record.get("temps") or {}).values()) if v is not None]


def _record_time(record: dict[str, Any]) -> datetime | None:
    try:
        ts = str(record.get("timestamp", "")).replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone()
    except ValueError:
        return None


def _age(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "?"
    sec = max(0, int((now - then).total_seconds()))
    if sec < 120:
        return f"{sec}s ago"
    if sec < 7200:
        return f"{sec // 60}m ago"
    return f"{sec / 3600:.1f}h ago"


def _get(record: dict[str, Any], *path: str) -> Any:
    cur: Any = record
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _gpu_field(record: dict[str, Any], i: int, field: str) -> Any:
    gpus = record.get("gpus") or []
    return gpus[i].get(field) if i < len(gpus) and isinstance(gpus[i], dict) else None


def _section(name: str, unicode: bool, color: bool, extra: str = "", width: int = TEXT_WIDTH) -> str:
    dash = "─" if unicode else "-"
    head = f"{dash * 2} {_paint(name, 'title', color)} "
    tail = f" {extra}" if extra else ""
    pad = max(2, width - len(name) - 4 - len(_strip_ansi(tail)))
    return head + _paint(dash * pad, "muted", color) + tail


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _level(value: Any, threshold: Any, key: str) -> str:
    """The value's own colour, or the warning colour at/over the card threshold."""
    v, t = _num(value), _num(threshold)
    return "warning" if v is not None and t is not None and v >= t else key


def _temp_key(value: Any, threshold: Any) -> str:
    v, t = _num(value), _num(threshold) or 60.0
    if v is None:
        return "muted"
    return "ok" if v < t - 10 else "temp_mid" if v < t else "error"


def _render_latest(
    record: dict[str, Any],
    unicode: bool,
    now: datetime,
    color: bool = False,
    thresholds: dict[str, Any] | None = None,
    width: int = TEXT_WIDTH,
) -> list[tuple[str, str]]:
    """Snapshot lines as (slot, line). The slot names the history sparkline that
    belongs beside the line in two-column mode ("" for none)."""
    th = thresholds or {}
    lines: list[tuple[str, str]] = []

    def add(line: str, slot: str = "") -> None:
        lines.append((slot, line))

    def sec(name: str, extra: str = "") -> str:
        return _section(name, unicode, color, extra, width)

    then = _record_time(record)
    when = then.strftime("%Y-%m-%d %H:%M:%S %Z") if then else "?"
    deg = "°C" if unicode else " C"
    add(sec("Snapshot", f"{when} " + _paint(f"({_age(then, now)})", "muted", color)), "@header")

    def row(label: str, percent: Any, detail: str, key: str, thr: Any = None, slot: str = "") -> None:
        pct = _paint(f"{_fmt(percent):>5}%", _level(percent, thr, key), color)
        add(
            _paint(f"{label:<10}", key, color)
            + f" {_bar(percent, unicode, key, color)} {pct}  {detail}",
            slot,
        )

    def gb(m: dict[str, Any], used: str = "used_gb", total: str = "total_gb") -> str:
        return _paint(f"{_fmt(m.get(used))} / {_fmt(m.get(total))} GB", "muted", color)

    cpu = record.get("cpu") or {}
    burst = _num(record.get("duration_sec")) or 0
    row("CPU", cpu.get("avg"), _paint(f"max {_fmt(cpu.get('max'))}%", "muted", color) if burst else "",
        "cpu", th.get("cpu_percent"), "cpu")
    spread = record.get("cpu_cores") or {}
    if spread:
        lo, med, hi = (spread.get(k) for k in ("min", "median", "max"))
        add(
            _paint(f"{'  cores':<10}", "cpu_cores", color)
            + f" {_range_bar(lo, med, hi, unicode, 'cpu_cores', color)} "
            + _paint(f"{_fmt(med):>5}%", "cpu_cores", color)
            + _paint(f"  median, range {_fmt(lo)}-{_fmt(hi)}%", "muted", color),
            "cpu_cores",
        )

    load = record.get("load") or {}
    cores = os.cpu_count() or 1
    load_pct = None if _num(load.get("min1")) is None else 100 * _num(load.get("min1")) / cores
    row(
        "Load",
        load_pct,
        "  ".join(
            _paint(f"{label} {_fmt(load.get(k), '.2f')}", key, color)
            for label, k, key in (("1m", "min1", "load"), ("5m", "min5", "load_5m"), ("15m", "min15", "load_15m"))
        )
        + _paint(f"  / {cores} cores", "muted", color),
        "load",
        slot="load",
    )
    for key, label in (("memory", "Memory"), ("swap", "Swap")):
        m = record.get(key) or {}
        row(label, m.get("percent"), gb(m), key, th.get(f"{key}_percent"), key)

    temps = record.get("temps") or {}
    gpus = record.get("gpus") or []
    if gpus:
        add(sec("GPUs"))
    for i, gpu in enumerate(gpus):
        idx = gpu.get("index")
        t = temps.get(f"GPU {idx}")
        temp = "" if t is None else _paint(f"{_fmt(t, '.0f')}{deg}", _temp_key(t, th.get("temp_celsius")), color)
        row(f"GPU {idx}", gpu.get("util_percent"), temp, "gpu", th.get("gpu_util_percent"), f"gpu{i}")
        row("  memory", gpu.get("mem_percent"), gb(gpu, "mem_used_gb", "mem_total_gb"),
            "gpu_mem", th.get("gpu_memory_percent"), f"gmem{i}")

    add(sec("I/O"))
    up, down = ("↑", "↓") if unicode else ("^", "v")
    net_thr, io_thr = th.get("network_mbps"), th.get("disk_io_mbps")
    io = [
        ("Network", down, "in", _get(record, "network", "in_mbps", "avg"), "net_in", net_thr),
        ("Network", up, "out", _get(record, "network", "out_mbps", "avg"), "net_out", net_thr),
        ("Disk I/O", up, "read", _get(record, "disk_io", "read_mbps", "avg"), "read", io_thr),
        ("Disk I/O", down, "write", _get(record, "disk_io", "write_mbps", "avg"), "write", io_thr),
    ]

    def io_cell(arrow: str, name: str, v: Any, k: str, thr: Any) -> str:
        return _paint(f"{arrow} {name:<5}", k, color) + _paint(f"{_fmt(v, '.2f'):>8}", _level(v, thr, k), color)

    for pair in (io[:2], io[2:]):
        add(
            _paint(f"{pair[0][0]:<10}", pair[0][4], color) + " "
            + "    ".join(io_cell(*c[1:]) for c in pair) + _paint("  MB/s", "muted", color),
            pair[0][4],
        )

    pings = [(t, p) for t, p in (record.get("ping") or {}).items() if isinstance(p, dict)]
    cells = []
    for i, (target, p) in enumerate(pings):
        key = f"ping_{i % 3}"
        avg, loss = _num(p.get("avg")), _num(p.get("loss")) or 0.0
        dot = _paint(f"{'●' if unicode else '*'} {target} ", key, color)
        if avg is None:
            cells.append(dot + _paint("DOWN", "error", color))
            continue
        cell = dot + _paint(f"{avg:.1f} ms", _level(avg, th.get("ping_ms"), key), color)
        if loss > 0:
            cell += _paint(f" {loss * 100:.0f}% loss", "warning", color)
        cells.append(cell)
    if cells:
        add(_paint(f"{'Ping':<10}", "ping", color) + " " + "   ".join(cells),
            f"ping:{pings[0][0]}")

    readings = _temps_of(record)
    if readings:
        add(sec(f"Temperatures ({deg.strip()})", f"{len(readings)} sensors"))
        thr = th.get("temp_celsius")
        add(
            "   ".join(
                _paint(f"{label} ", "muted", color) + _paint(f"{value:>3.0f}", _temp_key(value, thr), color)
                for label, value in (
                    ("min", min(readings)),
                    ("median", statistics.median(readings)),
                    ("mean", statistics.fmean(readings)),
                    ("max", max(readings)),
                )
            ),
            "maxt",
        )

    drives = record.get("drives") or []
    if drives:
        add(sec("Drives"))
    for d in drives:
        mount = str(d.get("mount", "?"))
        row(mount if len(mount) <= 10 else "…" + mount[-9:] if unicode else ".." + mount[-8:],
            d.get("percent"), gb(d), "drive", th.get("disk_percent"))
    return lines


TEXT_STATS_WIDTH: Final[int] = 23  # "    min    mean     max"
TEXT_AUTO_HOURS: Final[tuple[float, float]] = (1.0, 3.0)


def _text_layout(width: int, view: str, unicode: bool) -> tuple[int, bool, int]:
    """(width, two-column?, column width) for a requested report width."""
    width = max(TEXT_MIN_WIDTH, width)
    two_col = view == "both" and unicode and width >= TEXT_TWO_COLUMN_WIDTH
    return width, two_col, (width - 3) // 2 if two_col else width


def _spark_field_width(col_w: int) -> int:
    return max(10, col_w - 8 - 2 - TEXT_STATS_WIDTH)


def auto_history_hours(width: int, view: str, unicode: bool, record_interval: float) -> float:
    """Default window: as many records as the sparklines have columns, so each
    column is about one record, kept between 1 and 3 hours."""
    _, _, col_w = _text_layout(width, view, unicode)
    span = _spark_field_width(col_w) * max(1.0, record_interval) / 3600
    lo, hi = TEXT_AUTO_HOURS
    return round(min(hi, max(lo, span)), 2)


def _render_history(
    records: list[dict[str, Any]], unicode: bool, hours: float, rows: int,
    color: bool = False,
    thresholds: dict[str, Any] | None = None,
    table: bool = False,
    width: int = TEXT_WIDTH,
    now: datetime | None = None,
) -> tuple[str, list[tuple[str, str]], list[str]]:
    """(section header, sparkline rows as (slot, line), trailing lines).

    Rows follow the snapshot's order; slots match _render_latest's so two-column
    mode can pair each row with its snapshot line.
    """
    th = thresholds or {}
    labels = f"{'min':>7} {'mean':>7} {'max':>7}"
    header = _section(
        f"History, last {hours:g}h ({len(records)} records)", unicode, color,
        _paint(labels, "muted", color), width,
    )
    if not records:
        return header, [], [_paint("No history records yet.", "muted", color)]
    n_gpus = max((len(r.get("gpus") or []) for r in records), default=0)

    def series(fn) -> list[float | None]:
        return [_num(fn(r)) for r in records]

    def avg_min_max(*path: str) -> dict[str, list[float | None]]:
        return {
            "values": series(lambda r: _get(r, *path, "avg")),
            "lows": series(lambda r: _get(r, *path, "min")),
            "peaks": series(lambda r: _get(r, *path, "max")),
        }

    columns: list[_HistRow] = [
        _HistRow("cpu", "CPU%", spec=".1f", key="cpu", threshold=th.get("cpu_percent"),
                 scale="pct", **avg_min_max("cpu")),
        _HistRow("cpu_cores", "Cores", series(lambda r: _get(r, "cpu_cores", "median")), ".1f",
                 "cpu_cores", None, "pct"),
        _HistRow("load", "Load1", series(lambda r: _get(r, "load", "min1")), ".2f", "load", None, "load"),
        _HistRow("memory", "Mem%", series(lambda r: _get(r, "memory", "percent")), ".1f", "memory",
                 th.get("memory_percent"), "pct"),
        _HistRow("swap", "Swap%", series(lambda r: _get(r, "swap", "percent")), ".1f", "swap",
                 th.get("swap_percent"), "pct"),
    ]
    for i in range(n_gpus):
        columns.append(
            _HistRow(f"gpu{i}", f"GPU{i}%", series(lambda r, i=i: _gpu_field(r, i, "util_percent")),
                     ".0f", "gpu", th.get("gpu_util_percent"), "pct")
        )
        columns.append(
            _HistRow(f"gmem{i}", f"GMem{i}", series(lambda r, i=i: _gpu_field(r, i, "mem_percent")),
                     ".0f", "gpu_mem", th.get("gpu_memory_percent"), "pct")
        )
    for slot, name, path, key, thr in (
        ("net_in", "NetIn", ("network", "in_mbps"), "net_in", th.get("network_mbps")),
        ("net_out", "NetOut", ("network", "out_mbps"), "net_out", th.get("network_mbps")),
        ("read", "DiskR", ("disk_io", "read_mbps"), "read", th.get("disk_io_mbps")),
        ("write", "DiskW", ("disk_io", "write_mbps"), "write", th.get("disk_io_mbps")),
    ):
        columns.append(_HistRow(slot, name, spec=".2f", key=key, threshold=thr, scale="zero",
                                **avg_min_max(*path)))
    # The hottest sensor in each record.
    columns.append(
        _HistRow("maxt", "MaxT", [max(t) if t else None for t in map(_temps_of, records)], ".0f",
                 "temp", th.get("temp_celsius"), "temp")
    )

    # Sparkline field fills the width so the stats line up under the header labels.
    field_w = _spark_field_width(width)
    spark_w = field_w
    # Real time axis: the window ends now and spans `hours`.
    end_dt = now or datetime.now(timezone.utc).astimezone()
    end = end_dt.timestamp()
    start = end - hours * 3600
    rec_times = [t.timestamp() if t else None for t in map(_record_time, records)]
    # Hold a value across empty columns for up to 1.5 record intervals.
    interval = getattr(APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL)
    hold = 1.5 * max(float(interval), (end - start) / spark_w)
    chars = _SPARK_CHARS if unicode else _SPARK_ASCII
    spark_rows: list[tuple[str, str]] = []
    for row in columns:
        slot, name, vals, spec, key, thr = row[:6]
        nums = [v for v in vals if v is not None]
        # min / max come from the bursts' own extremes when records keep them,
        # so a short spike shows in "max" even though the average hides it.
        lows = [v for v in (row.lows or []) if v is not None] or nums
        peaks = [v for v in (row.peaks or []) if v is not None] or nums

        def cell(v: float, shade: str) -> str:
            return _paint(f"{format(v, spec):>7}", _level(v, thr, shade), color)

        stats = (
            " ".join(
                cell(v, shade)
                for v, shade in (
                    (min(lows), "stat_min"),
                    (sum(nums) / len(nums), "stat_mean"),
                    (max(peaks), "title"),
                )
            )
            if nums
            else _paint(f"{'no data':>{TEXT_STATS_WIDTH}}", "muted", color)
        )
        spark = _paint(
            f"{_sparkline(_time_buckets(rec_times, vals, start, end, spark_w, hold), chars, *row.bounds()):<{field_w}}",
            key, color,
        )
        spark_rows.append((slot, f"{_paint(f'{name:<7}', key, color)} {spark}  {stats}"))

    # Time axis under the sparklines: window start, middle and end (now).
    fmt = "%m-%d %H:%M" if hours > 20 else "%H:%M"

    def label(ts: float) -> str:
        return datetime.fromtimestamp(ts, end_dt.tzinfo).strftime(fmt)

    if spark_w >= 3:
        rule = ("└" + "─" * (spark_w - 2) + "┘") if unicode else ("+" + "-" * (spark_w - 2) + "+")
    else:
        rule = ("─" if unicode else "-") * spark_w
    first, last = label(start), label(end)
    ticks = first + last.rjust(max(0, spark_w - len(first)))
    mid = label((start + end) / 2)
    if spark_w >= len(first) + len(mid) + len(last) + 4:
        at = spark_w // 2 - len(mid) // 2
        ticks = ticks[:at] + mid + ticks[at + len(mid):]
        # A tick on the rule over the middle label's centre.
        centre = at + len(mid) // 2
        rule = rule[:centre] + ("┬" if unicode else "+") + rule[centre + 1:]
    deg = "°C" if unicode else "C"
    tail = [
        " " * 8 + _paint(rule, "muted", color),
        " " * 8 + _paint(ticks, "muted", color),
        _paint(f"Blank = no records. Net/Disk MB/s, MaxT = hottest sensor {deg}.", "muted", color),
    ]
    if not table:
        return header, spark_rows, tail

    # Evenly sample at most `rows` records, always including the newest.
    count = len(records)
    picks = sorted({round(k * (count - 1) / max(1, rows - 1)) for k in range(min(rows, count))})
    sep = " │ " if unicode else " | "
    table_w = 14 + len(sep) + 7 * len(columns) - 1
    tail.append("")
    tail.append(
        f"{'Time':<14}" + sep + " ".join(_paint(f"{c.name[:6]:>6}", c.key, color) for c in columns)
    )
    tail.append(("─" if unicode else "-") * table_w)
    for idx in picks:
        t = _record_time(records[idx])
        stamp = t.strftime("%m-%d %H:%M:%S") if t else "?"
        cells = " ".join(
            _paint(f"{_fmt(c.values[idx], c.spec):>6}", "warning", color)
            if _level(c.values[idx], c.threshold, "") == "warning"
            else f"{_fmt(c.values[idx], c.spec):>6}"
            for c in columns
        )
        tail.append(_paint(f"{stamp:<14}", "muted", color) + _paint(sep, "muted", color) + cells)
    return header, spark_rows, tail


def render_text(
    records: list[dict[str, Any]],
    *,
    style: str = "unicode",
    view: str = "both",
    hours: float = 1,
    rows: int = 24,
    title: str = "",
    color: bool | str = False,
    thresholds: dict[str, Any] | None = None,
    table: bool = False,
    live: dict[str, Any] | None = None,
    width: int = TEXT_WIDTH,
    now: datetime | None = None,
) -> str:
    """Render history records as a console report (pure; unit-tested).

    The snapshot is `live` (a fresh sample) when given, else the newest record.
    """
    unicode = style == "unicode"
    now = now or datetime.now(timezone.utc).astimezone()
    heading = title or socket.gethostname()
    host = socket.gethostname()
    sub = "" if heading == host else (f"· {host}" if unicode else f"- {host}")
    inner = f" {heading} " + _paint(sub, "muted", color) + " "
    width, two_col, col_w = _text_layout(width, view, unicode)
    span = width - 2
    hint = "options and API: /help "
    fill = max(1, span - len(_strip_ansi(inner)) - len(hint))
    if unicode:
        out = [
            _paint("╭" + "─" * span + "╮", "cpu", color),
            _paint("│", "cpu", color) + _paint(inner[: len(heading) + 2], "title", color)
            + inner[len(heading) + 2 :] + " " * fill + _paint(hint, "muted", color)
            + _paint("│", "cpu", color),
            _paint("╰" + "─" * span + "╯", "cpu", color),
        ]
    else:
        title_line = inner.strip()
        out = ["=" * width, title_line + hint.rstrip().rjust(width - len(_strip_ansi(title_line))), "=" * width]
    left: list[tuple[str, str]] = []
    if view in ("latest", "both"):
        if live or records:
            left = _render_latest(live or records[-1], unicode, now, color, thresholds, col_w)
        else:
            left = [("", "No data yet.")]
    hist: tuple[str, list[tuple[str, str]], list[str]] | None = None
    if view in ("history", "both"):
        hist = _render_history(records, unicode, hours, rows, color, thresholds, table, col_w, now)

    if two_col and hist:
        # Snapshot on the left, history on the right. Each history row goes beside the snapshot line with its slot when that
        # line is still ahead, else on the next free line; the legend follows.
        header, spark_rows, tail = hist
        index = {slot: i for i, (slot, _) in enumerate(left) if slot}
        right = [header] + [""] * (len(left) - 1)
        pos = 1
        for slot, line in spark_rows + [("", t) for t in tail[:3]]:
            at = index.get(slot, -1)
            at = at if at >= pos else pos
            while at < len(right) and right[at]:
                at += 1
            if at >= len(right):
                right += [""] * (at - len(right) + 1)
            right[at] = line
            pos = at + 1
        extra = right[len(left):]
        rows_left = [line for _, line in left] + [""] * len(extra)
        for a, b in zip(rows_left, right):
            out.append(a + " " * max(0, col_w - len(_strip_ansi(a))) + "   " + b)
        out += tail[3:]  # the optional table, full width below
    else:
        out += [line for _, line in left]
        if left and hist:
            out.append("")
        if hist:
            header, spark_rows, tail = hist
            out += [header] + [line for _, line in spark_rows] + tail
    return "\n".join(out) + "\n"


TEXT_USAGE: Final[str] = (
    "usage: /text?view=latest|history|both&style=unicode|ascii|simple&color=auto|on|off"
    "&hours=N&width=80&height=N&live=0|1&table=0|1&rows=24  (see /help)\n"
)

HELP_TEXT: Final[str] = """\
Horatio: system monitor for this machine. Plain-text help, for people and agents.

QUICK START
  curl "http://<host>:8080/text?width=$COLUMNS"   # colour report: live snapshot + history
  curl http://<host>:8080/text?style=simple      # plain ASCII, no colour (best for agents/logs)
  curl -fsS http://<host>:8080/tui.py | python3 -   # live, auto-updating view
  python3 tui.py                                 # the same, from a horatio checkout on the box

LIVE VIEW (tui.py)
  Standard library only (Python 3.8+, Linux/macOS). Detects terminal size,
  colour depth (NO_COLOR, FORCE_COLOR, COLORTERM, TERM) and UTF-8, and redraws
  every 2 s (--interval). Keys, listed at the bottom: q quit, r refresh,
  v cycle view, t table, +/- history window. --color on|256|16|off and --style override the
  detection; --once prints a single frame. Piping from curl runs whatever the
  server sends: fine on your own network. Otherwise download, read, then run:
    curl -fsS -o tui.py http://<host>:8080/tui.py && python3 tui.py
  From a horatio checkout: python3 tui.py on the box itself (finds the local
  server), or python3 tui.py http://<host>:8080 (or set HORATIO_URL).

/text  console report built from the history file
  view   both (default) | latest | history
         latest  = a live snapshot, sampled when you request it
         history = per-metric sparklines (incl. max temperature) over the window
  style  unicode (default: bars, sparklines, box drawing)
         | ascii (7-bit characters)
         | simple (ascii with no colour)
  color  auto (default: on for unicode, off for ascii/simple) | on | 256 | 16 | off
         on is 24-bit ANSI; 256/16 for older terminals; turn it off when parsing
  hours  history window (0.1 to 8760). Default: 1 to 3 hours, picked so the
         sparklines have about one record per column (wider terminals and
         slower recording intervals get the longer window)
  width  terminal width in columns, default 80 (60 to 400). The server can't see
         your terminal, so pass it:  curl "http://<host>:8080/text?width=$COLUMNS"
         At 160 or wider, the unicode report shows snapshot and history side by side.
  height trim to N lines (drops the table first, then cuts with a note)
  live   0 to use the newest history record instead of sampling now
  table  1 to add a table of evenly sampled records (default off)
  rows   max table rows, default 24 (1 to 500)
  Bad parameters return 400 with a usage line.

  Reading the report:
  - The snapshot is sampled live (one instant reading; rates are Glances'
    latest values). History records are written every history.record_interval
    seconds (600 by default), and each one averages several samples.
  - Temperatures: min / median / mean / max over every sensor (CPU, drives,
    GPUs, ...). The MaxT history row is the hottest sensor in each record.
  - Percentages are 0-100. Network and disk I/O are MB/s (megabytes). Temperatures
    are Celsius. Ping is round-trip ms; DOWN means every probe was lost.
  - Load: the bar is the 1-minute load divided by the CPU core count.
  - Sparklines are on a real time axis, window start (left) to now (right);
    a blank stretch means no records then (box off, recorder down). Each sparkline
    has a fixed scale: percentages 0-100, rates and disk from 0 to the peak, load
    from 0 to the core count (or the peak), MaxT from 20 C. Beside it: min, mean
    and max; for CPU, network and disk, min/max are the extremes within each
    record's sampling burst, so short spikes show even when averaged away.
  - "-" means no data (a metric that is missing from that record).
  - Values at or over the thresholds in /config.json are drawn orange.

JSON API (machine-readable; prefer these for exact values)
  GET /api/history?hours=24      records (no temps/processes/ip): cpu, memory, swap,
                                 gpus, drives, network, disk_io, load, ping, cpu_freq
  GET /api/history/field/temps   [{timestamp, temps}] for the same range (?hours=)
  GET /api/history/field/processes   per-process rows, when process logging is on
  GET /api/history/settings      recording interval and sampling settings
  GET /api/processlist           live process list (arguments stripped)
  GET /api/gpu-info              live NVIDIA GPU details
  GET /api/gpu-processes         live processes holding GPU memory
  GET /api/cpu-info              CPU model and core counts
  GET /api/ping                  ping the configured targets now
  GET /api/sensors               fans/temps/power snapshot (root collector, when installed)
  GET /api/storage-health        drive SMART/RAID/ECC snapshot (root collector)
  GET /api/4/<plugin>            live Glances metrics: cpu, mem, memswap, load,
                                 network, diskio, fs, sensors, gpu
  GET /config.json               title, ping targets, thresholds

Dashboard: http://<host>:8080/   This page: /help
"""


@APP.get("/text")
async def text_view(
    view: str = "both",
    style: str = "unicode",
    color: str = "auto",
    hours: float | None = None,
    rows: int = 24,
    table: bool = False,
    width: int = TEXT_WIDTH,
    height: int | None = None,
    live: bool = True,
) -> Response:
    """Plain-text report for terminals: `curl http://<host>:8080/text`.

    style=simple is ascii without colour. Colour defaults on (24-bit) for
    unicode and off otherwise; color=on|256|16|off overrides. height=N trims the
    report to N lines (the TUI passes its terminal size). live=0 uses the newest
    record instead of a fresh sample. Documented at /help.
    """
    if (
        view not in ("latest", "history", "both")
        or style not in ("ascii", "unicode", "simple")
        or color not in ("auto", "on", "256", "16", "off")
    ):
        return Response(content=TEXT_USAGE, status_code=400, media_type="text/plain")
    if hours is None:
        hours = auto_history_hours(
            width, view, style == "unicode",
            getattr(APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL),
        )
    hours = max(0.1, min(hours, 8760))
    rows = max(1, min(rows, 500))
    result = await asyncio.to_thread(_read_history_cached, hours)
    if isinstance(result, str):
        return Response(
            content=f"Failed to read history: {result}\n",
            status_code=500,
            media_type="text/plain",
        )
    records, _skipped, _truncated = result
    sample: dict[str, Any] | None = None
    if live and view in ("latest", "both"):
        try:
            sample = await _live_sample()
        except Exception as e:
            logger.warning("/text: live sample failed, using the newest record: %s", e)
    if style == "simple":
        style, color = "ascii", "off"
    depth: bool | str = {"on": "24", "256": "256", "16": "16", "off": False}.get(
        color, "24" if style == "unicode" else False
    )

    def render(with_table: bool) -> str:
        return render_text(
            records,
            style=style,
            view=view,
            hours=hours,
            rows=rows,
            title=str(_CONFIG.get("title") or ""),
            color=depth,
            thresholds=_CONFIG.get("thresholds") or {},
            table=with_table,
            live=sample,
            width=max(TEXT_MIN_WIDTH, min(width, 400)),
        )

    body = render(table)
    if height is not None:
        height = max(5, height)
        if table and body.count("\n") > height:
            # Asked for the table: shrink it to the rows that fit before cutting.
            rows = max(3, rows - (body.count("\n") - height))
            body = render(True)
        body = _fit_height(body, height, depth)
    return Response(content=body, media_type="text/plain; charset=utf-8")


def _fit_height(body: str, height: int, color: bool | str) -> str:
    """Trim a report to `height` lines, noting how many were cut."""
    lines = body.rstrip("\n").split("\n")
    if len(lines) <= height:
        return body
    cut = len(lines) - (height - 1)
    return "\n".join(lines[: height - 1] + [_paint(f"... {cut} more lines (taller terminal, or another view)", "muted", color)]) + "\n"


# Short-lived caches so several terminals polling /text every second cost one
# live sample per second and one history parse per new record.
_LIVE_TTL: Final[float] = 1.0
_live_cache: dict[str, Any] = {"at": 0.0, "record": None}
_live_lock = asyncio.Lock()
_history_cache: dict[str, Any] = {"key": None, "result": None}


async def _live_sample() -> dict[str, Any]:
    async with _live_lock:
        if _live_cache["record"] is None or time.monotonic() - _live_cache["at"] > _LIVE_TTL:
            _live_cache["record"] = await collect_history_sample(APP.state.glances_url, live=True)
            _live_cache["at"] = time.monotonic()
        return _live_cache["record"]


def _read_history_cached(hours: float) -> tuple[list[dict[str, Any]], int, bool] | str:
    """_read_history, reused until the file changes or a minute passes (the
    window start moves). Callers must not mutate the returned records."""
    try:
        st = HISTORY_FILE.stat()
        key = (hours, st.st_mtime_ns, st.st_size, int(time.time() // 60))
    except OSError:
        key = None
    if key is not None and _history_cache["key"] == key:
        return _history_cache["result"]
    result = _read_history(hours)
    if key is not None and not isinstance(result, str):
        _history_cache.update(key=key, result=result)
    return result


@APP.get("/tui.py")
async def tui_script(request: Request) -> Response:
    """The live terminal client, so it runs without a checkout:
    curl -fsS http://<host>:8080/tui.py | python3 -
    The copy served has SERVED_URL set to the address it was fetched from, so
    it connects back here with no argument."""
    source = (Path(__file__).parent / "tui.py").read_text()
    base = _base_url(request)
    source = source.replace('SERVED_URL = ""', f"SERVED_URL = {base!r}", 1)
    # The usage comments too, so someone who just curls it sees commands that work.
    source = source.replace("http://<host>:8080", base)
    return Response(content=source, media_type="text/x-python; charset=utf-8")


def _base_url(request: Request) -> str:
    """This server as the client reached it (its Host header), e.g. http://box:8080."""
    return str(request.base_url).rstrip("/")


@APP.get("/help")
async def help_page(request: Request) -> Response:
    """Plain-text guide to /text and the JSON API, with this server's real
    address in the examples so they can be pasted as-is."""
    body = HELP_TEXT.replace("http://<host>:8080", _base_url(request))
    return Response(content=body, media_type="text/plain; charset=utf-8")


@APP.get("/")
async def index() -> FileResponse:
    """Serve index.html."""
    return FileResponse(INDEX_FILE)


@APP.get("/config.json")
async def config_json() -> FileResponse:
    """Serve dashboard configuration."""
    return FileResponse(CONFIG_FILE)


@APP.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Log unhandled exceptions and return 500."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return Response(
        content=json.dumps({"error": "Internal server error"}),
        status_code=500,
        media_type="application/json",
    )


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Check if a port is available for binding.

    SO_REUSEADDR, as uvicorn sets it: without it the probe fails for a minute
    after a restart because the previous server's client connections are still
    in TIME_WAIT, and systemd then loops on "already in use" with nobody listening.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def get_local_ip() -> str | None:
    """Get the local IP address used for LAN communication."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Connect to external IP (doesn't send data, just determines route)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def main(port: int, glances_port: int, host: str = DEFAULT_HOST) -> None:
    """Start Glances and the dashboard server."""
    setup_logging()

    # Check if ports are available
    if not is_port_available(port):
        logger.error("Port %d is already in use", port)
        sys.exit(1)

    if not is_port_available(glances_port, host="127.0.0.1"):
        logger.error("Glances port %d is already in use", glances_port)
        sys.exit(1)

    # Store config in app state for lifespan and proxy route
    APP.state.glances_url = f"http://127.0.0.1:{glances_port}"
    APP.state.glances_port = glances_port

    logger.info("Starting dashboard server on port %d", port)
    logger.info("  Local:   http://localhost:%d", port)
    local_ip: str | None = get_local_ip()
    if local_ip:
        logger.info(
            "  Network: http://%s:%d  <-- use this from other devices", local_ip, port
        )

    uvicorn.run(APP, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Homelab dashboard server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to run the dashboard on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--glances-port",
        type=int,
        default=DEFAULT_GLANCES_PORT,
        help=f"Port for Glances backend (default: {DEFAULT_GLANCES_PORT})",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Address to bind the dashboard to (default: {DEFAULT_HOST}, all "
            "interfaces). Use 127.0.0.1 to restrict access to this machine."
        ),
    )
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="Set the history settings password and exit",
    )
    args: argparse.Namespace = parser.parse_args()

    if args.set_password:
        pw = getpass.getpass("Enter new password: ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Passwords do not match.")
            sys.exit(1)
        PASSWORD_FILE.write_text(_hash_password(pw) + "\n")
        # Restrict permissions — hash is sensitive (offline brute-force target)
        PASSWORD_FILE.chmod(0o600)
        print(f"Password saved to {PASSWORD_FILE}")
        sys.exit(0)

    main(port=args.port, glances_port=args.glances_port, host=args.host)
