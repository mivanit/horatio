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

Run: uv run python python/server.py   (config.json must exist; ./install.sh creates it)
"""

from __future__ import annotations

import _thread
import argparse
import asyncio
import email
import email.message
import email.policy
import getpass
import hashlib
import hmac
import json
import logging
import mailbox
import math
import os
import re
import secrets
import shlex
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Reversible,
    Sequence,
)
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Final,
    Literal,
    NamedTuple,
    NoReturn,
    TypeAlias,
    TypeVar,
)

import anyio
import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

import hostcheck
import hwmon
import zfs

SOURCE_DIR: Final[Path] = Path(__file__).resolve().parent
ROOT_DIR: Final[Path] = SOURCE_DIR.parent

STATIC_DIR: Final[Path] = ROOT_DIR / "static"
INDEX_FILE: Final[Path] = ROOT_DIR / "index.html"
THEMES_DIR: Final[Path] = ROOT_DIR / "themes"
# Theme names as the page's loader (index.html) and lua/install-openwrt.sh accept them;
# "default" is the picker's no-theme entry, so a default.css could never be chosen.
THEME_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")
LOGS_DIR: Final[Path] = ROOT_DIR / "logs"
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
# One line per round of service checks (http_targets). Its own file: the checks
# run every minute, history records every ten. Never rotated, like history.jsonl;
# the name must not match scripts/history-merge.sh's history-*.jsonl.gz glob.
SERVICES_FILE: Final[Path] = LOGS_DIR / "services.jsonl"
PASSWORD_FILE: Final[Path] = ROOT_DIR / ".password"
CONFIG_FILE: Final[Path] = ROOT_DIR / "config.json"


def _read_stamp(path: Path) -> str | None:
    """A version file's text, stripped, or None when it is missing or empty."""
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


# VERSION is the release (docs/operations.md: Versions and updates); .build is the commit
# install.sh saw (`git describe`), so a box tracking main tells its pulls apart.
VERSION: Final[str] = _read_stamp(ROOT_DIR / "VERSION") or "unknown"
BUILD: Final[str | None] = _read_stamp(ROOT_DIR / ".build")
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
    e: json.JSONDecodeError
    try:
        cfg: Any = json.loads(CONFIG_FILE.read_text())
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
    value: Any = cfg.get(key, default)
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


def _cfg_num(
    cfg: dict[str, Any],
    key: str,
    default: float,
    lo: float,
    hi: float,
    section: str,
) -> float:
    """Read a numeric (int or float) setting, like _cfg_int. The range check
    also rejects inf and NaN (every comparison with them is False)."""
    value: Any = cfg.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not (lo <= value <= hi)
    ):
        logging.getLogger("dashboard").warning(
            "config.json %s.%s=%r is invalid or outside [%g, %g]; using %g",
            section,
            key,
            value,
            lo,
            hi,
            default,
        )
        return float(default)
    return float(value)


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
# Upper bound on the window any history read accepts. A long window parses up to
# HISTORY_MAX_TAIL_READ of JSON into memory (hundreds of MB of Python objects),
# and /text keeps that result in _history_cache; a small box can lower it.
HISTORY_MAX_HOURS: Final[float] = _cfg_num(
    _history_cfg, "max_hours", 8760, 0.1, 8760, "history"
)
_processes_enabled: Any = _history_cfg.get("processes_enabled", False)
if not isinstance(_processes_enabled, bool):
    logging.getLogger("dashboard").warning(
        "config.json history.processes_enabled=%r is not a boolean; using false",
        _processes_enabled,
    )
    _processes_enabled = False
DEFAULT_HISTORY_PROCESSES_ENABLED: Final[bool] = _processes_enabled


def _read_limits(cfg: dict[str, Any]) -> tuple[float, float]:
    """(network_mbps, disk_io_mbps) ceilings from config.json's "limits".

    Glances' rates are a counter delta over its own elapsed time and now and then
    come out absurd (counter wrap, interface churn). Each interface's and each
    drive's samples are clamped to these when a record is collected (_RateCaps);
    totals are not capped. Records from before the caps are clamped when served
    (clamp_record_rates). Read once at startup.
    Defaults: 10 Gbit/s ~ 1250 MB/s; a PCIe 5 NVMe ~ 15 GB/s.
    """
    limits: Any = cfg.get("limits", {})
    if not isinstance(limits, dict):
        logging.getLogger("dashboard").warning(
            "config.json: 'limits' is not an object; using defaults"
        )
        limits = {}
    return (
        _cfg_num(limits, "network_mbps", 1250.0, 1, 1e9, "limits"),
        _cfg_num(limits, "disk_io_mbps", 15000.0, 1, 1e9, "limits"),
    )


_LIMITS: Final[tuple[float, float]] = _read_limits(_CONFIG)
MAX_NETWORK_MBPS: Final[float] = _LIMITS[0]
MAX_DISK_IO_MBPS: Final[float] = _LIMITS[1]


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

# Persistent clients avoid constructing an SSL context on every poll. Recording
# has its own pool so browser traffic cannot use up its Glances connections.
_glances_client_shared: httpx.AsyncClient | None = None
_glances_client_recording: httpx.AsyncClient | None = None


@asynccontextmanager
async def _glances_client(
    *, recording: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """Borrow the selected pool, or own a temporary client outside lifespan."""
    shared: httpx.AsyncClient | None = (
        _glances_client_recording if recording else _glances_client_shared
    )
    if shared is not None:
        yield shared
        return
    client: httpx.AsyncClient
    async with httpx.AsyncClient() as client:
        yield client


@asynccontextmanager
async def _glances_clients() -> AsyncIterator[None]:
    """Own both pools for startup, service operation, and task shutdown."""
    global _glances_client_shared, _glances_client_recording
    limits: httpx.Limits = httpx.Limits(
        max_connections=20, max_keepalive_connections=10
    )
    async with (
        httpx.AsyncClient(limits=limits) as interactive,
        httpx.AsyncClient(limits=limits) as recording,
    ):
        _glances_client_shared = interactive
        _glances_client_recording = recording
        try:
            yield
        finally:
            _glances_client_shared = None
            _glances_client_recording = None


_T = TypeVar("_T")
# Server-side result caches (see _cached): key -> (monotonic time, value).
_cache_values: dict[str, tuple[float, Any]] = {}
_cache_locks: dict[str, asyncio.Lock] = {}


async def _cached(key: str, ttl: float, compute: Callable[[], Awaitable[_T]]) -> _T:
    """``compute()``'s result, reused for ``ttl`` seconds.

    Every open browser polls the same endpoints, and several of them start
    processes (ping, nvidia-smi) or call out (the public-IP lookup): without
    this each extra tab repeats that work. Concurrent callers share one
    computation, and a failure is raised to its caller and not kept. The value
    is shared between callers, so they must not change it.
    """
    hit: tuple[float, Any] | None = _cache_values.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    async with _cache_locks.setdefault(key, asyncio.Lock()):
        hit = _cache_values.get(key)  # filled while this caller waited
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value: _T = await compute()
        _cache_values[key] = (time.monotonic(), value)
        return value


# Rate limiting for manual history recording
# Minimum gap between manual "record now" requests; 0 disables the limit. Read
# from config.json like the other history settings so it survives restarts.
DEFAULT_MANUAL_RECORD_MIN_INTERVAL: Final[int] = _cfg_int(
    _history_cfg, "min_record_interval", 60, 0, 3600
)
_last_manual_record_time: float = 0.0


def _hash_password(password: str) -> str:
    """Generate salt:hash string for a password using PBKDF2-HMAC-SHA256."""
    salt: bytes = secrets.token_bytes(16)
    hash_bytes: bytes = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{salt.hex()}:{hash_bytes.hex()}"


def _verify_password(password: str) -> bool:
    """Verify password against stored PBKDF2 hash. Returns False if no password file."""
    if not PASSWORD_FILE.exists():
        return False
    try:
        stored: str = PASSWORD_FILE.read_text().strip()
        salt_hex: str
        hash_hex: str
        salt_hex, hash_hex = stored.split(":", 1)
        salt: bytes = bytes.fromhex(salt_hex)
        expected_hash: bytes = bytes.fromhex(hash_hex)
    except (ValueError, FileNotFoundError):
        return False
    except OSError as e:  # e.g. written with sudo: root-owned, unreadable here
        logger.error("Cannot read %s: %s", PASSWORD_FILE, e)
        return False

    computed: bytes = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(computed, expected_hash)


# Throttle the password-verify endpoints. The PBKDF2 check is deliberately
# expensive (PBKDF2_ITERATIONS), so an unauthenticated client must not be able to
# spam it (CPU exhaustion) or brute-force freely. Track consecutive failures per
# client IP and lock that IP out for a spell once it crosses the threshold.
AUTH_MAX_FAILS: Final[int] = 5
AUTH_LOCKOUT_SEC: Final[int] = 60
_auth_failures: dict[
    str, tuple[int, float]
] = {}  # ip -> (consecutive_fails, locked_until)
# One KDF at a time: a burst of guesses must not fill the thread pool that the
# recorder and the threaded cards share.
_kdf_lock: asyncio.Lock = asyncio.Lock()


async def _verify_password_throttled(
    request: Request, password: str
) -> Response | None:
    """Verify ``password`` rate-limited per client IP.

    Returns an error Response to send back — 429 while the IP is locked out, 403 on
    a wrong password — or None when the password is correct. A correct password
    clears the IP's failure streak; the lockout is what makes the costly KDF
    unspammable.
    """
    ip: str = request.client.host if request.client else "unknown"
    now: float = time.time()
    fails: int
    until: float
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
    # Count the attempt before the KDF runs, so concurrent guesses each see the
    # earlier ones and the lockout holds under a burst; a correct one clears it.
    fails += 1
    _auth_failures[ip] = (
        fails,
        now + AUTH_LOCKOUT_SEC if fails >= AUTH_MAX_FAILS else 0.0,
    )
    async with _kdf_lock:
        ok: bool = await asyncio.to_thread(_verify_password, password)
    if not ok:
        return Response(
            content=json.dumps({"error": "Incorrect password"}),
            status_code=403,
            media_type="application/json",
        )
    _auth_failures.pop(ip, None)
    return None


def _sanitize_process_argv(argv: list[str]) -> dict[str, str]:
    """Expose an executable and a known script/module position, never option
    operands or inline code. Unknown interpreter flags keep the executable
    alone: guessing the first non-flag can publish a password or a -c/-e body.
    """
    exe: str = argv[0]
    exe_name: str = Path(exe).name
    executable: dict[str, str] = {"name": exe_name, "path": exe}
    args: list[str] = argv[1:]

    def script(path: str) -> dict[str, str]:
        return (
            executable
            if path == "-"
            else {"name": Path(path).name, "path": f"{exe} {path}"}
        )

    if re.fullmatch(r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?t?", exe_name.lower()):
        i: int = 0
        while i < len(args):
            arg: str = args[i]
            if arg == "--":
                return script(args[i + 1]) if i + 1 < len(args) else executable
            if arg == "-m":
                module: str = args[i + 1] if i + 1 < len(args) else ""
                if module and all(part.isidentifier() for part in module.split(".")):
                    return {"name": module, "path": f"{exe} -m {module}"}
                return executable
            if not arg.startswith("-"):
                return script(arg)
            if arg in {
                "-b",
                "-bb",
                "-B",
                "-d",
                "-E",
                "-i",
                "-I",
                "-O",
                "-OO",
                "-P",
                "-q",
                "-R",
                "-s",
                "-S",
                "-u",
                "-v",
                "-vv",
                "-x",
            }:
                i += 1
            elif arg in {"-W", "-X", "--check-hash-based-pycs"}:
                i += 2  # their operands are not scripts (and can contain secrets)
            elif arg.startswith(("-W", "-X")) and len(arg) > 2:
                i += 1
            else:
                return executable  # includes -c, stdin, and unknown options
        return executable

    interpreter: re.Match[str] | None = re.fullmatch(
        r"(node|nodejs|ruby|perl|php)(?:\.?\d+(?:\.\d+)*)?", exe_name.lower()
    )
    if interpreter:
        kind: str = interpreter.group(1)
        harmless_flags: set[str] = {
            "node": {"--no-warnings", "--trace-warnings", "--enable-source-maps"},
            "nodejs": {"--no-warnings", "--trace-warnings", "--enable-source-maps"},
            "ruby": {"-w", "-W0", "-W1", "-W2", "--disable-gems"},
            "perl": {"-w", "-W", "-T", "-t"},
            "php": {"-n", "-q"},
        }[kind]
        for i, arg in enumerate(args):
            if arg == "--" or (kind == "php" and arg == "-f"):
                return script(args[i + 1]) if i + 1 < len(args) else executable
            if not arg.startswith("-"):
                return script(arg)
            if arg not in harmless_flags:
                return executable  # includes eval/print code and option operands
    return executable


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
    # PID 0 has no cmdline; nethogs uses it for traffic it could not tie to a
    # process ("unknown TCP/0/0"), so it is not the kernel's.
    if pid == 0:
        return {"name": "unattributed", "path": ""}

    e: OSError | IndexError | ValueError
    try:
        cmdline_raw: str = Path(f"/proc/{pid}/cmdline").read_text()
        # Keep empty operands: dropping the value in `-W '' -c code` shifts
        # -c into the warning option and makes its code look like a script. Strip
        # every trailing NUL: setproctitle (celery, postgres) writes its title,
        # often the full command line, over argv and pads the rest with NULs, so
        # one NUL less would leave empty "arguments" and skip the split below.
        cmdline: list[str] = cmdline_raw.rstrip("\0").split("\0") if cmdline_raw else []

        # Fallback: if only 1 element with spaces, it's space-separated (e.g., Firefox snap)
        if len(cmdline) == 1 and " " in cmdline[0]:
            cmdline = cmdline[0].split()

        if not cmdline:
            logger.debug("PID %d: cmdline empty", pid)
            return {"name": fallback_name, "path": fallback_path}

        return _sanitize_process_argv(cmdline)

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
            # Sensors come from hwmon.py, which the server serves at /api/4/sensors.
            "--disable-plugin",
            "sensors",
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


# nvidia-smi --query-gpu field -> the key _parse_gpu_stats gives it.
GPU_STAT_KEYS: Final[dict[str, str]] = {
    "index": "index",
    "utilization.gpu": "util_percent",
    "memory.used": "memory_used_mb",
    "memory.total": "memory_total_mb",
    "temperature.gpu": "temperature",
    "power.draw": "power_draw",
    "temperature.memory": "temperature_memory",
    "clocks_event_reasons_counters.sw_thermal_slowdown": "throttle_sw_thermal_us",
    "clocks_event_reasons_counters.hw_thermal_slowdown": "throttle_hw_thermal_us",
    # Percent of the fan's maximum (nvidia-smi gives no RPM); N/A on passive cards.
    "fan.speed": "fan_percent",
}
# The order of the older queries, for a caller that does not say.
GPU_STAT_FIELDS: Final[tuple[str, ...]] = tuple(GPU_STAT_KEYS)[:9]


def _parse_gpu_stats(
    stdout: str, fields: Sequence[str] = GPU_STAT_FIELDS
) -> list[dict[str, Any]]:
    """Parse nvidia-smi --query-gpu CSV whose columns are ``fields`` (in order).

    Each metric field degrades to None on '[N/A]'/'[Not Supported]' (nvidia-smi's
    tokens for unreadable fields) rather than dropping the whole GPU row; only an
    unparseable index drops the row, since the index is the GPU's key. A shorter
    line (an older query) leaves the missing fields None. Pure (no I/O) so the
    parsing, including the N/A handling, is unit-testable.
    """

    def _f(s: str) -> float | None:
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    gpus: list[dict[str, Any]] = []
    line: str
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            logger.warning("Skipping malformed nvidia-smi GPU line: %s", line)
            continue
        try:
            index: int = int(parts[0])
        except (ValueError, TypeError):
            logger.warning("Skipping nvidia-smi GPU line with bad index: %s", line)
            continue
        gpu: dict[str, Any] = {key: None for key in GPU_STAT_KEYS.values()}
        name: str
        part: str
        for name, part in zip(fields, parts):
            gpu[GPU_STAT_KEYS[name]] = _f(part)
        gpu["index"] = index
        if "fan.speed" not in fields:
            del gpu["fan_percent"]  # not asked for: absent, not unreadable
        gpus.append(gpu)
    return gpus


_nvidia_logged: set[str] = set()


def _log_nvidia_failure(result: subprocess.CompletedProcess[str]) -> None:
    """Log a failed nvidia-smi once per distinct message, not on every poll.
    nvidia-smi prints its errors on stdout (a driver/library mismatch: exit 18),
    so both streams are read."""
    message: str = (
        result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    ).splitlines()[0]
    if message in _nvidia_logged:
        return
    _nvidia_logged.add(message)
    if hostcheck.MISMATCH_TEXT in message:
        message += " (the driver was updated under the running module: reboot)"
    logger.warning("nvidia-smi failed (exit %d): %s", result.returncode, message)


def get_gpu_stats() -> list[dict[str, Any]]:
    """GPU utilization, memory, and temperature from nvidia-smi (driver-native).

    History sources GPU metrics from nvidia-smi rather than Glances because
    Glances' pynvml binding returns all-null for some datacenter GPUs (e.g. the
    Tesla P40) while the driver's own CLI reads them fine. Per-field
    None-preserving; empty list on failure (nvidia-smi missing/timeout/error).
    """
    basic: list[str] = [
        "index",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "temperature.gpu",
        "power.draw",
        "temperature.memory",
        "fan.speed",
    ]
    counters: tuple[str, str] = (
        "clocks_event_reasons_counters.sw_thermal_slowdown",
        "clocks_event_reasons_counters.hw_thermal_slowdown",
    )
    basic_fields: str = ",".join(basic)
    fields: list[str] = [*basic, *counters]
    command: list[str] = [
        "nvidia-smi",
        "--query-gpu=" + basic_fields + "," + ",".join(counters),
        "--format=csv,noheader,nounits",
    ]
    e: Exception
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Pre-575 nvidia-smi rejects the entire query for these newer counters.
        # Retry only that diagnostic; keep valid memory/temperature/power data.
        diagnostic: str = result.stdout + result.stderr
        if result.returncode != 0 and any(
            f'Field "{field}" is not a valid field to query' in diagnostic
            for field in counters
        ):
            command = [
                "nvidia-smi",
                "--query-gpu=" + basic_fields,
                "--format=csv,noheader,nounits",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            fields = basic
        if result.returncode != 0:
            _log_nvidia_failure(result)
            return []
        return _parse_gpu_stats(result.stdout, fields)
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


def _read_ping_targets(cfg: dict[str, Any]) -> list[str]:
    """The ping_targets addresses, in config order; an entry without an address
    is skipped and null means the defaults, as in the frontend. Any other shape
    is fatal like _load_config's errors rather than skipped: the frontend reads
    the raw config.json and reads ``t.address`` of each entry, so a server that
    limped on would still serve a broken ping card, and a bare string here used
    to die at import with a traceback."""
    targets: Any = cfg.get("ping_targets")
    if targets is None:
        targets = [{"address": "8.8.8.8"}, {"address": "192.168.1.1"}]
    if not isinstance(targets, list) or not all(
        isinstance(t, dict) and isinstance(t.get("address") or "", str) for t in targets
    ):
        sys.exit(
            f'{CONFIG_FILE}: ping_targets must be a list of {{"address": "<host>"}}'
            f" objects, got {json.dumps(targets)}"
        )
    return [t["address"] for t in targets if t.get("address")]


PING_TARGETS: Final[list[str]] = _read_ping_targets(_CONFIG)


# ============================================================================
# Service checks: is each http_targets URL answering, and how often has it been
# ============================================================================


class HttpTarget(NamedTuple):
    """One http_targets entry."""

    name: str
    url: str
    status: int | None  # the exact status required; None = anything below 400
    insecure: bool  # skip TLS certificate verification


def _read_http_targets(cfg: dict[str, Any]) -> list[HttpTarget]:
    """The http_targets entries, in config order; absent or null means none
    (the feature is off). A wrong shape is fatal, like ping_targets: the
    frontend reads the same raw list."""
    raw: Any = cfg.get("http_targets")
    if raw is None:
        return []

    def bad(why: str) -> NoReturn:
        sys.exit(
            f'{CONFIG_FILE}: http_targets must be a list of {{"name": "<label>", '
            f'"url": "http(s)://..."}} objects (optional "status": <code>, '
            f'"insecure": true): {why}'
        )

    if not isinstance(raw, list):
        bad(f"got {json.dumps(raw)}")
    targets: list[HttpTarget] = []
    entry: Any
    for entry in raw:
        if not isinstance(entry, dict):
            bad(f"got entry {json.dumps(entry)}")
        name: Any = entry.get("name")
        url: Any = entry.get("url")
        status: Any = entry.get("status")
        insecure: Any = entry.get("insecure", False)
        if not isinstance(name, str) or not name.strip():
            bad(f"entry without a name: {json.dumps(entry)}")
        if not isinstance(url, str) or not re.match(r"https?://[^/\s]", url):
            bad(f"{name!r} has no http:// or https:// url")
        if status is not None and (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            bad(f"{name!r} status must be an HTTP status code, got {status!r}")
        if not isinstance(insecure, bool):
            bad(f"{name!r} insecure must be true or false, got {insecure!r}")
        if any(t.name == name for t in targets):
            bad(f"two entries are named {name!r}")
        targets.append(HttpTarget(name, url, status, insecure))
    return targets


HTTP_TARGETS: Final[list[HttpTarget]] = _read_http_targets(_CONFIG)
_services_cfg: dict[str, Any] = _CONFIG.get("services", {})
if not isinstance(_services_cfg, dict):
    logging.getLogger("dashboard").warning(
        "config.json: 'services' is not an object; using defaults"
    )
    _services_cfg = {}
# Seconds between rounds of checks, and the total time one request may take.
SERVICES_CHECK_INTERVAL: Final[int] = _cfg_int(
    _services_cfg, "check_interval", 60, 10, 3600, "services"
)
SERVICES_TIMEOUT: Final[float] = _cfg_num(
    _services_cfg, "timeout", 5, 1, 30, "services"
)
# A failed check is repeated once after this long before it counts as down, so
# a service that is mid-restart is not recorded as an outage.
SERVICES_RETRY_DELAY: Final[float] = 2.0
# The Services card's uptime windows (hours); the longest is what is kept in memory.
SERVICES_UPTIME_HOURS: Final[tuple[int, ...]] = (1, 24, 168)
# Most columns /api/services/history returns: about one per few pixels of a card.
SERVICES_MAX_BUCKETS: Final[int] = 300

# A round of checks reduced to what uptime needs: (epoch, {target name: passed}).
ServiceRound: TypeAlias = tuple[float, dict[str, bool]]
# The rounds of the last SERVICES_UPTIME_HOURS[-1] hours, oldest first: filled
# from the file's tail at startup, then by the checker. Only the event loop
# touches these two.
_services_rounds: deque[ServiceRound] = deque()
# Target name -> its newest result ({ok, status, ms?, error?}) plus "checked".
_services_latest: dict[str, dict[str, Any]] = {}
# When the checker started (epoch), None until it first runs: with no round on
# record yet, staleness counts from here. Never reset, so a checker that has
# died reads as stale.
_services_started: float | None = None


async def _check_http_target(
    client: httpx.AsyncClient, target: HttpTarget, timeout: float
) -> dict[str, Any]:
    """One GET of a target: {ok, status, ms} or {ok: False, status, error}.
    Only the status line and headers are read, never the body, and ``timeout``
    bounds the whole request including redirects (httpx's own timeout is per
    phase)."""

    async def fetch() -> int:
        resp: httpx.Response
        # An expected 3xx is the target's own answer: following it would
        # never see that status.
        follow: bool = not (target.status is not None and 300 <= target.status < 400)
        async with client.stream(
            "GET", target.url, timeout=timeout, follow_redirects=follow
        ) as resp:
            return resp.status_code

    started: float = time.monotonic()
    e: Exception
    try:
        status: int = await asyncio.wait_for(fetch(), timeout)
    # asyncio.TimeoutError is not builtin TimeoutError before Python 3.11.
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "status": None, "error": "timeout"}
    # Any exception is this target's failure: one escaping here would fail the
    # whole round's gather and freeze every target at its last result.
    except Exception as e:
        if not isinstance(e, (httpx.HTTPError, httpx.InvalidURL, OSError)):
            logger.exception("Service check of %s failed unexpectedly", target.name)
        return {"ok": False, "status": None, "error": type(e).__name__}
    ms: int = round((time.monotonic() - started) * 1000)
    ok: bool = status == target.status if target.status is not None else status < 400
    if ok:
        return {"ok": True, "status": status, "ms": ms}
    return {"ok": False, "status": status, "error": f"status {status}"}


async def check_http_targets(
    targets: Sequence[HttpTarget],
    client: httpx.AsyncClient,
    insecure_client: httpx.AsyncClient,
    timeout: float,
    retry_delay: float = SERVICES_RETRY_DELAY,
) -> dict[str, dict[str, Any]]:
    """One round: every target at once, a failure retried once. Name -> result."""

    async def one(target: HttpTarget) -> dict[str, Any]:
        chosen: httpx.AsyncClient = insecure_client if target.insecure else client
        result: dict[str, Any] = await _check_http_target(chosen, target, timeout)
        if not result["ok"]:
            await asyncio.sleep(retry_delay)
            result = await _check_http_target(chosen, target, timeout)
        return result

    results: list[dict[str, Any]] = await asyncio.gather(*(one(t) for t in targets))
    return {t.name: r for t, r in zip(targets, results)}


def _service_round(ts: float, record: dict[str, Any]) -> ServiceRound | None:
    """A services.jsonl record as a ServiceRound; None when it is not one."""
    targets: Any = record.get("targets")
    if not isinstance(targets, dict):
        return None
    return ts, {
        name: r.get("ok") is True for name, r in targets.items() if isinstance(r, dict)
    }


def _note_service_round(record: dict[str, Any], ts: float) -> None:
    """Add a just-finished round to the in-memory window and drop what has aged
    out of the longest uptime window."""
    parsed: ServiceRound | None = _service_round(ts, record)
    if parsed is None:
        return
    _services_rounds.append(parsed)
    cutoff: float = ts - SERVICES_UPTIME_HOURS[-1] * 3600
    while _services_rounds and _services_rounds[0][0] < cutoff:
        _services_rounds.popleft()
    name: str
    result: dict[str, Any]
    for name, result in record["targets"].items():
        _services_latest[name] = {**result, "checked": record["timestamp"]}


def services_uptime(
    rounds: Iterable[ServiceRound],
    names: Sequence[str],
    now: float,
    windows: Sequence[int] = SERVICES_UPTIME_HOURS,
) -> dict[str, dict[str, dict[str, int]]]:
    """Per target and window (hours, as a string key): {"up": passed rounds,
    "checks": rounds that checked it}. Time with no rounds (Horatio down, or the
    target not configured yet) counts as neither up nor down."""
    out: dict[str, dict[str, dict[str, int]]] = {
        name: {str(w): {"up": 0, "checks": 0} for w in windows} for name in names
    }
    ts: float
    oks: dict[str, bool]
    for ts, oks in rounds:
        age: float = now - ts
        live: list[str] = [str(w) for w in windows if 0 <= age <= w * 3600]
        if not live:
            continue
        name: str
        for name in names:
            ok: bool | None = oks.get(name)
            if ok is None:
                continue
            key: str
            for key in live:
                out[name][key]["checks"] += 1
                out[name][key]["up"] += ok
    return out


def services_down_since(
    rounds: Reversible[ServiceRound], names: Sequence[str]
) -> dict[str, float | None]:
    """For each target whose newest check failed, the time (epoch) of the first
    round of its current unbroken run of failures; None for a target that is up
    or was never checked. Rounds that did not check a target are skipped. Only
    what is in memory counts, so the answer is at most SERVICES_UPTIME_HOURS[-1]
    hours back."""
    out: dict[str, float | None] = {name: None for name in names}
    undecided: set[str] = set(names)
    ts: float
    oks: dict[str, bool]
    for ts, oks in reversed(rounds):
        name: str
        for name in list(undecided):
            ok: bool | None = oks.get(name)
            if ok is None:
                continue
            if ok:
                undecided.discard(name)
            else:
                out[name] = ts
        if not undecided:
            break
    return out


def services_bucket_plan(
    hours: float, now: float, interval: int
) -> tuple[float, int, int]:
    """(start epoch, bucket seconds, bucket count) for a history grid of the last
    ``hours``: at most SERVICES_MAX_BUCKETS buckets, each a whole number of check
    intervals, on boundaries that do not move between requests."""
    span: float = hours * 3600
    bucket: int = max(1, math.ceil(span / SERVICES_MAX_BUCKETS / interval)) * interval
    count: int = max(1, math.ceil(span / bucket))
    end: float = (math.floor(now / bucket) + 1) * bucket
    return end - count * bucket, bucket, count


def bucket_services(
    rounds: Iterable[ServiceRound],
    names: Sequence[str],
    start: float,
    bucket: int,
    count: int,
) -> dict[str, list[list[int]]]:
    """Per target, ``count`` [passed, failed] pairs, one per ``bucket`` seconds
    from ``start``. [0, 0] is a bucket with no checks."""
    out: dict[str, list[list[int]]] = {
        name: [[0, 0] for _ in range(count)] for name in names
    }
    ts: float
    oks: dict[str, bool]
    for ts, oks in rounds:
        i: int = int((ts - start) // bucket)
        if not 0 <= i < count:
            continue
        name: str
        ok: bool
        for name, ok in oks.items():
            if name in out:
                out[name][i][0 if ok else 1] += 1
    return out


def _run_ping(target: str = "8.8.8.8", timeout: int = 2) -> float | None:
    """Run a single ping and return latency in ms, or None on failure."""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        if result.returncode != 0:
            return None
        # Parse "time=12.3 ms" from ping output
        line: str
        for line in result.stdout.split("\n"):
            if "time=" in line:
                part: str
                for part in line.split():
                    if part.startswith("time="):
                        return float(part.split("=")[1])
        return None
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return None


# ============================================================================
# History Recording
# ============================================================================


def _parse_gpu_index(gpu: dict[str, Any]) -> int:
    """Extract numeric GPU index from Glances gpu_id (e.g. 'nvidia0' -> 0)."""
    glances_id: Any = gpu.get("gpu_id", 0)
    if isinstance(glances_id, str) and glances_id.startswith("nvidia"):
        try:
            return int(glances_id.replace("nvidia", ""))
        except ValueError:
            return 0
    return glances_id if isinstance(glances_id, int) else 0


def _num(value: object) -> float | None:
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


def _round(value: object, ndigits: int) -> float | None:
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
        size: Any
        used: Any
        size, used = fs.get("size"), fs.get("used")
        if (
            isinstance(size, (int, float))
            and isinstance(used, (int, float))
            and size > 0
        ):
            used = round(used / (size * 0.001))  # bucket: 0.1% of the fs
        return (os.path.realpath(fs["device_name"]), size, used)

    def _rank(mnt: str) -> tuple[bool, int, str]:
        return (mnt == "", len(mnt), mnt)  # non-empty first, then shortest

    # One key per row, computed once: realpath walks the filesystem.
    keys: list[tuple[str, Any, Any] | None] = [
        _key(fs) if fs.get("device_name") else None for fs in fs_entries
    ]
    winner: dict[tuple[str, Any, Any], str] = {}
    fs: dict[str, Any]
    key: tuple[str, Any, Any] | None
    for fs, key in zip(fs_entries, keys):
        if key is None:
            continue
        mnt: Any = fs.get("mnt_point") or ""
        if key not in winner or _rank(mnt) < _rank(winner[key]):
            winner[key] = mnt

    kept: list[dict[str, Any]] = []
    used: set[tuple[str, Any, Any]] = set()
    for fs, key in zip(fs_entries, keys):
        if key is None:
            kept.append(fs)  # nothing to dedupe against
            continue
        if key not in used and (fs.get("mnt_point") or "") == winner[key]:
            used.add(key)
            kept.append(fs)
    return kept


def _visible_mounts(mountinfo: str) -> dict[str, str]:
    """Mount point -> fs type of the mounts a path lookup can reach, from
    /proc/self/mountinfo text.

    The service's sandbox (ProtectHome=tmpfs) lays an empty tmpfs over /home; a
    disk mounted at or under /home is still listed by psutil, but statvfs on it
    reports the tmpfs, so Glances shows the wrong size. A mount is hidden when
    another mount sits at its path or an enclosing one and is not one of its
    ancestors (mountinfo's parent ids): mounted over it, not under it. Line order
    can't be used: mounts made in the initramfs (/sys, /proc, a separate /usr) are
    listed before / after the switch to the real root.

    TODO: keyed by mount point and fs type only, so a disk covered by another of
    the same type at the same path (ext4 over ext4 at /mnt) keeps its row and
    shows the covering disk's numbers; matching the source device too would fix it.
    """
    mounts: dict[
        str, tuple[str, str, str]
    ] = {}  # id -> (parent id, mount point, fs type)
    line: str
    for line in mountinfo.splitlines():
        fields: list[str]
        rest: list[str]
        fields, rest = (part.split() for part in line.split(" - ", 1))
        # Mount points escape space, tab, newline and backslash as \ooo.
        mnt: str = re.sub(r"\\([0-7]{3})", lambda o: chr(int(o.group(1), 8)), fields[4])
        mounts[fields[0]] = (fields[1], mnt, rest[0])

    def ancestors(mount_id: str) -> set[str]:
        seen: set[str] = set()
        while mount_id in mounts and mount_id not in seen:
            seen.add(mount_id)
            mount_id = mounts[mount_id][0]
        return seen

    # Mount ids by mount point ("/" as ""), so each mount is checked against the
    # mounts at its own path and its parent directories only, not against all.
    at_path: dict[str, list[str]] = {}
    mount_id: str
    for mount_id, (_, mnt, _) in mounts.items():
        at_path.setdefault(mnt.rstrip("/"), []).append(mount_id)

    visible: dict[str, str] = {}
    fstype: str
    for mount_id, (_, mnt, fstype) in mounts.items():
        below: set[str] = ancestors(mount_id)
        path: str = mnt.rstrip("/")
        hidden: bool = False
        while True:
            if any(other not in below for other in at_path.get(path, ())):
                hidden = True
                break
            if not path:
                break
            path = path.rpartition("/")[0]
        if not hidden:
            visible[mnt] = fstype
    return visible


def _drop_hidden_mounts(fs_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop fs rows whose mount is covered by another mount (see _visible_mounts):
    their size and usage would be the covering fs's, not the disk's."""
    with open("/proc/self/mountinfo") as f:
        visible: dict[str, str] = _visible_mounts(f.read())
    return [
        fs
        for fs in fs_entries
        if visible.get(fs.get("mnt_point", "")) == fs.get("fs_type")
    ]


def _add_inode_usage(fs_entries: list[dict[str, Any]]) -> None:
    """Set inodes_used/inodes_percent on each fs row, from statvfs of its mount.

    Glances doesn't report inodes. None when the fs has no fixed inode table
    (btrfs, vfat report f_files=0) or the mount is gone (OSError), so one vanished
    mount can't fail the whole request.
    """
    fs: dict[str, Any]
    for fs in fs_entries:
        st: os.statvfs_result | None
        try:
            st = os.statvfs(fs["mnt_point"])
        except OSError:
            st = None
        used: int | None = st.f_files - st.f_ffree if st and st.f_files else None
        fs["inodes_used"] = used
        fs["inodes_percent"] = (
            round(used / st.f_files * 100, 1) if st and used is not None else None
        )


def _shape_fs(
    fs_entries: list[dict[str, Any]], resolve_devices: bool
) -> list[dict[str, Any]]:
    """Glances' fs rows as served and recorded: duplicate views of one device
    collapsed, hidden mounts dropped, inode usage added; with ``resolve_devices``
    each device_name symlink is resolved first (the UI matches it to diskio).
    Sync, for a thread: every step reads the filesystem."""
    if resolve_devices:
        entry: dict[str, Any]
        for entry in fs_entries:
            device_name: str = entry.get("device_name", "")
            if device_name:
                entry["device_name"] = os.path.realpath(device_name)
    fs_entries = _drop_hidden_mounts(_dedupe_fs_by_device(fs_entries))
    _add_inode_usage(fs_entries)
    return fs_entries


def _sum_known(values: list[float | None]) -> float | None:
    """Sum of the known values; None when none is known, so a sample with no
    readings is a gap in compute_stats rather than a fake 0."""
    known: list[float] = [v for v in values if v is not None]
    return sum(known) if known else None


def compute_stats(samples: Sequence[object]) -> dict[str, float | None]:
    """min/avg/max over the finite samples, or all-None when there are none.

    None/non-finite samples are dropped (an unreadable sample is not a zero); an
    empty window yields None stats (unknown), never a misleading {0, 0, 0}. A real
    idle sample of 0.0 is kept and averages normally.
    """
    vals: list[float] = [v for v in (_num(s) for s in samples) if v is not None]
    if not vals:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(vals) / len(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def aggregate_cpu_cores(
    percpu_samples: dict[int, list[object]],
) -> dict[str, float] | None:
    """Cross-core spread: min/median/max across per-core burst averages.

    ``percpu_samples`` maps core index -> that core's ``total`` samples over the
    burst. Each core is first averaged over the burst (dropping None/non-finite
    samples via ``_num``); the spread is then taken across those per-core averages.
    Returns None when no core had any finite sample (so the caller omits the key).
    """
    core_avgs: list[float] = []
    samples: list[object]
    for samples in percpu_samples.values():
        vals: list[float] = [v for v in (_num(s) for s in samples) if v is not None]
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
    used_mb: float | None = _num(st.get("memory_used_mb"))
    total_mb: float | None = _num(st.get("memory_total_mb"))
    used_gb: float | None = _round(used_mb / 1024, 2) if used_mb is not None else None
    total_gb: float | None = (
        _round(total_mb / 1024, 2) if total_mb is not None else None
    )
    percent: float | None = (
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
    glances_util: dict[int, float | None] = {
        _parse_gpu_index(g): _num(g.get("proc")) for g in (gpu_data or [])
    }

    gpus: list[dict[str, Any]] = []
    if gpu_stats:
        st: dict[str, Any]
        for st in gpu_stats:
            idx: Any = st.get("index")
            if idx is None:
                continue
            util: float | None = _num(st.get("util_percent"))
            if util is None:  # nvidia-smi had no util; try Glances for this GPU
                util = glances_util.get(idx)
            sw_us: float | None = _num(st.get("throttle_sw_thermal_us"))
            hw_us: float | None = _num(st.get("throttle_hw_thermal_us"))
            gpus.append(
                {
                    "index": idx,
                    "util_percent": _round(util, 1),
                    **_gpu_mem_fields(st),
                    # Seconds of thermal slowdown since driver load (cumulative:
                    # a rise between two records is slowdown in that interval).
                    "throttle_sw_thermal_s": _round(sw_us / 1e6, 1)
                    if sw_us is not None
                    else None,
                    "throttle_hw_thermal_s": _round(hw_us / 1e6, 1)
                    if hw_us is not None
                    else None,
                }
            )
    elif gpu_data:
        # nvidia-smi unavailable: Glances utilization only, memory unknown.
        gpu: dict[str, Any]
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
        st: dict[str, Any]
        for st in gpu_stats:
            idx: Any = st.get("index")
            if idx is not None:
                temps[f"GPU {idx}"] = _round(st.get("temperature"), 1)
                if st.get("temperature_memory") is not None:
                    temps[f"GPU {idx} mem"] = _round(st.get("temperature_memory"), 1)
    elif gpu_data:
        gpu: dict[str, Any]
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
    target: str
    attempts: int
    for target, attempts in ping_attempts.items():
        vals: list[float] = [
            v for v in (_num(s) for s in ping_samples.get(target, [])) if v is not None
        ]
        if vals:
            failed: int = max(0, attempts - len(vals))
            loss: float = round(failed / attempts, 4) if attempts else 0.0
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
    path: Path
    for path in CPUFREQ_DIR.glob("policy*/scaling_cur_freq"):
        try:
            freqs.append(int(path.read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return freqs


def _cpu_freq_limits() -> dict[str, float | None]:
    """The hardware clock range (MHz) from policy0, None when unreadable."""
    out: dict[str, float | None] = {}
    key: str
    name: str
    for key, name in (("min_mhz", "cpuinfo_min_freq"), ("max_mhz", "cpuinfo_max_freq")):
        try:
            out[key] = int((CPUFREQ_DIR / "policy0" / name).read_text().strip()) / 1000
        except (OSError, ValueError):
            out[key] = None
    return out


def aggregate_cpu_freq(
    freq_samples: Sequence[object], limits: dict[str, float | None] | None = None
) -> dict[str, float | None] | None:
    """min/median/avg/max MHz over every (core, sample) reading of the burst.

    Per-core readings are pooled rather than averaged first: the point is to see
    whether some cores sit at idle clocks while others boost, which an all-core
    mean (Glances' quicklook ``cpu_hz_current``) blurs away. None when nothing
    was readable, so the caller omits the key; the hardware range rides along.
    """
    vals: list[float] = sorted(
        v for v in (_num(s) for s in freq_samples) if v is not None
    )
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
        stat: str = Path(f"/proc/{pid}/stat").read_text()
        # The comm field is in parentheses and may itself contain spaces/parens.
        fields: list[str] = stat[stat.rindex(")") + 2 :].split()
        ppid: int = int(fields[1])
        start_ticks: int = int(fields[19])
        create_time: float | None = (
            round(_BOOT_TIME + start_ticks / _CLK_TCK, 2)
            if _BOOT_TIME is not None
            else None
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
    ``io_counters`` over its own refresh interval; ``gpu_util_percent`` is the
    SM utilization summed over the GPUs the process uses (nvidia-smi pmon), left
    out when the driver had no sample. Rows are sparse: a key whose
    value is unknown (None) or a rate that is zero is left out, so a row carries
    only ``pid`` and ``name`` for sure. Sorted by CPU, GPU-only rows last.
    """
    gpu_by_pid: dict[int, dict[str, Any]] = {}
    g: dict[str, Any]
    for g in gpu_processes:
        pid: Any = g.get("pid")
        if not isinstance(pid, int):
            continue
        slot: dict[str, Any] = gpu_by_pid.setdefault(
            pid, {"mem": 0.0, "util": None, "gpus": set(), "name": g.get("name")}
        )
        slot["mem"] += _num(g.get("gpu_memory_mib")) or 0.0
        util: float | None = _num(g.get("gpu_util_percent"))
        if util is not None:  # summed over the GPUs it runs on (like CPU over cores)
            slot["util"] = (slot["util"] or 0.0) + util
        if isinstance(g.get("gpu_index"), int):
            slot["gpus"].add(g["gpu_index"])

    valid: list[dict[str, Any]] = [
        p
        for p in processes
        if isinstance(p, dict)
        and isinstance(p.get("pid"), int)
        and not isinstance(p["pid"], bool)
    ]

    def cpu(p: dict[str, Any]) -> float:
        return _num(p.get("cpu_percent")) or 0.0

    def rss_bytes(p: dict[str, Any]) -> float | None:
        info: Any = p.get("memory_info")
        return _num(info.get("rss")) if isinstance(info, dict) else None

    def rss(p: dict[str, Any]) -> float:
        return rss_bytes(p) or 0.0

    def disk_kbps(p: dict[str, Any]) -> tuple[float | None, float | None]:
        # Glances: io_counters = [read, write, read_old, write_old, io_tag] cumulative
        # bytes, io_tag 1 when readable; the rate is over time_since_update.
        io: Any = p.get("io_counters")
        dt: float | None = _num(p.get("time_since_update"))
        if not (isinstance(io, list) and len(io) >= 5 and io[4] == 1 and dt):
            return None, None
        read: float | None
        write: float | None
        read_old: float | None
        write_old: float | None
        read, write, read_old, write_old = (_num(x) for x in io[:4])
        if read is None or write is None or read_old is None or write_old is None:
            return None, None
        return (read - read_old) / dt / 1024, (write - write_old) / dt / 1024

    def sparse(row: dict[str, Any], rates: tuple[str, ...] = ()) -> dict[str, Any]:
        # Drop unknowns, and the rate keys when zero or negative (counter reset).
        return {
            k: v
            for k, v in row.items()
            if v is not None
            and not (k in rates and isinstance(v, (int, float)) and v <= 0)
        }

    chosen: dict[int, dict[str, Any]] = {}
    p: dict[str, Any]
    for p in sorted(valid, key=cpu, reverse=True)[:count]:
        chosen.setdefault(p["pid"], p)
    for p in sorted(valid, key=rss, reverse=True)[:count]:
        chosen.setdefault(p["pid"], p)
    for p in valid:
        if p["pid"] in gpu_by_pid:
            chosen.setdefault(p["pid"], p)

    rows: list[dict[str, Any]] = []
    for pid, p in chosen.items():
        info: dict[str, str] = get_sanitized_process_info(
            pid, str(p.get("name") or "unknown")
        )
        ppid: int | None
        create_time: float | None
        ppid, create_time = _proc_parent_and_start(pid)
        rd: float | None
        wr: float | None
        rd, wr = disk_kbps(p)
        memory_bytes: float | None = rss_bytes(p)
        row: dict[str, Any] = {
            "pid": pid,
            "ppid": ppid,
            "create_time": create_time,
            "name": info["name"],
            "user": p.get("username"),
            "status": p.get("status"),
            "cpu_percent": _round(p.get("cpu_percent"), 1),
            "rss_mb": _round(memory_bytes / (1024**2), 1)
            if memory_bytes is not None
            else None,
            "threads": p.get("num_threads")
            if isinstance(p.get("num_threads"), int)
            else None,
            "disk_read_kbps": _round(rd, 1),
            "disk_write_kbps": _round(wr, 1),
        }
        gpu: dict[str, Any] | None = gpu_by_pid.get(pid)
        if gpu and gpu["mem"] > 0:
            row["gpu_mem_mib"] = round(gpu["mem"], 1)
            row["gpus"] = sorted(gpu["gpus"])
            if gpu["util"]:
                row["gpu_util_percent"] = round(gpu["util"], 1)
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
                    "gpu_util_percent": round(gpu["util"], 1) if gpu["util"] else None,
                    "gpus": sorted(gpu["gpus"]),
                }
            )
        )
    rows.sort(key=lambda r: ("cpu_percent" not in r, -r.get("cpu_percent", 0.0)))
    return rows


async def collect_history_sample(
    glances_url: str, live: bool = False
) -> dict[str, Any]:
    """Collect a single history sample from all data sources.

    Takes samples over the configured duration and computes min/avg/max
    for volatile metrics. ``live`` (the /text snapshot) takes one immediate
    sample with no process list or IP lookup, so a request returns in about a
    second instead of a whole sample window, and never calls out to ipify.
    """
    sample_duration: int = 0 if live else APP.state.history_sample_duration
    sample_count: int = 1 if live else APP.state.history_sample_count
    sample_interval: float = sample_duration / max(1, sample_count)

    cpu_samples: list[object] = []
    # Per-core CPU usage across the burst, keyed by core index: {idx: [total, ...]}
    percpu_samples: dict[int, list[object]] = {}
    net_in_samples: list[float | None] = []
    net_out_samples: list[float | None] = []
    # Caps each interface / drive rate sample and remembers which went over
    # (see _RateCaps); summarised into the record's `rate_capped` note.
    caps: _RateCaps = _RateCaps()
    # Disk I/O, one dict per sample iteration: {disk_name: (read_mbps, write_mbps)}.
    # Keyed per iteration (not per disk) so a device that appears or vanishes
    # mid-window cannot shift another device's samples out of alignment.
    disk_iter_samples: list[dict[str, tuple[float | None, float | None]]] = []
    # Per-core clocks, pooled over the burst (see aggregate_cpu_freq).
    freq_samples: list[float] = []
    processes_enabled: bool = APP.state.history_processes_enabled and not live
    process_count: int = APP.state.history_process_count
    ping_enabled: bool = APP.state.ping_enabled
    ping_samples: dict[str, list[float]] = (
        {t: [] for t in PING_TARGETS} if ping_enabled else {}
    )
    ping_attempts: dict[str, int] = {t: 0 for t in PING_TARGETS} if ping_enabled else {}

    async def ping_round(i: int) -> None:
        """Ping every target once, recording each success in ping_samples."""
        # Count an attempt per target up front so a gather-timeout (which
        # skips the append below) still counts as a loss, not a non-event.
        target: str
        for target in PING_TARGETS:
            ping_attempts[target] += 1
        e: Exception
        try:
            # Live (/text) pings get a short leash: the request waits on
            # this, and a dead target would otherwise hold it 2-8 s.
            ping_results: list[float | None] = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        asyncio.to_thread(_run_ping, t, 1 if live else 2)
                        for t in PING_TARGETS
                    )
                ),
                timeout=2.5 if live else 8,
            )
            latency: float | None
            for target, latency in zip(PING_TARGETS, ping_results):
                if latency is not None:
                    ping_samples[target].append(latency)
        except Exception as e:
            logger.warning("Ping sample %d failed: %s", i, e)

    # Collect samples over time
    burst_start: float = time.monotonic()
    client: httpx.AsyncClient
    async with _glances_client(recording=not live) as client:
        i: int
        for i in range(sample_count):
            e: Exception
            try:
                # Fetch CPU
                cpu_resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/cpu", timeout=5.0
                )
                if cpu_resp.status_code == 200:
                    cpu_data: dict[str, Any] = cpu_resp.json()
                    # Raw value (may be None if Glances reports null); compute_stats
                    # drops non-numeric samples so this never crashes the record.
                    cpu_samples.append(cpu_data.get("total"))

                # Fetch per-core CPU (for the cross-core spread record). Keyed by
                # enumerate index to match the live per-core chart (app.js).
                percpu_resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/percpu", timeout=5.0
                )
                if percpu_resp.status_code == 200:
                    percpu_data: list[dict[str, Any]] = percpu_resp.json()
                    idx: int
                    core: dict[str, Any]
                    for idx, core in enumerate(percpu_data):
                        percpu_samples.setdefault(idx, []).append(core.get("total"))

                # Per-core clock frequencies (sysfs; [] without cpufreq)
                freq_samples.extend(await asyncio.to_thread(_read_cpu_freqs))

                # Fetch network I/O
                net_resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/network", timeout=5.0
                )
                if net_resp.status_code == 200:
                    net_data: list[dict[str, Any]] = net_resp.json()
                    # MB/s (match live view), summed over every interface but
                    # loopback. Each interface is capped on its own: a counter
                    # glitch is per interface, while the sum legitimately
                    # exceeds one link's rate (bridges, veths and tunnels count
                    # the same traffic again), so the total is never capped.
                    rates_in: list[float | None] = []
                    rates_out: list[float | None] = []
                    iface: dict[str, Any]
                    for iface in net_data:
                        name: str = iface.get("interface_name") or ""
                        if name.startswith("lo"):
                            continue
                        rates_in.append(
                            caps.sample(
                                iface.get("bytes_recv_rate_per_sec"),
                                MAX_NETWORK_MBPS,
                                ("network.in_mbps", name),
                            )
                        )
                        rates_out.append(
                            caps.sample(
                                iface.get("bytes_sent_rate_per_sec"),
                                MAX_NETWORK_MBPS,
                                ("network.out_mbps", name),
                            )
                        )
                    net_in_samples.append(_sum_known(rates_in))
                    net_out_samples.append(_sum_known(rates_out))
                else:
                    logger.warning(
                        "Network API returned status %d", net_resp.status_code
                    )

                # Fetch disk I/O
                disk_resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/diskio", timeout=5.0
                )
                if disk_resp.status_code == 200:
                    disk_data: list[dict[str, Any]] = disk_resp.json()
                    # Per-drive disk I/O; the whole-box totals are summed from
                    # these after the loop, once the mounted devices are known.
                    this_iter: dict[str, tuple[float | None, float | None]] = {}
                    disk: dict[str, Any]
                    for disk in disk_data:
                        disk_name: str | None = disk.get("disk_name")
                        if disk_name:
                            # Capped per device, like the network interfaces.
                            this_iter[disk_name] = (
                                caps.sample(
                                    disk.get("read_bytes_rate_per_sec"),
                                    MAX_DISK_IO_MBPS,
                                    ("disk_io.read_mbps", disk_name),
                                ),
                                caps.sample(
                                    disk.get("write_bytes_rate_per_sec"),
                                    MAX_DISK_IO_MBPS,
                                    ("disk_io.write_mbps", disk_name),
                                ),
                            )
                    disk_iter_samples.append(this_iter)
                else:
                    logger.warning(
                        "Disk IO API returned status %d", disk_resp.status_code
                    )

            except Exception as e:
                # TODO: track which metrics failed and include in record for frontend to indicate partial data
                logger.warning("Sample %d collection error: %s", i, e)

            # Ping (thread pool) concurrently with the wait for the next sample.
            # The wait is paced against the burst's start, so neither the
            # fetches above nor a dead target's 2 s timeout add to the burst:
            # it lasts about sample_duration, not up to twice that. The last
            # sample needs no wait (each Glances rate covers the slot before it).
            next_at: float = burst_start + (i + 1) * sample_interval
            await asyncio.gather(
                ping_round(i) if ping_enabled else asyncio.sleep(0),
                asyncio.sleep(
                    max(0.0, next_at - time.monotonic()) if i < sample_count - 1 else 0
                ),
            )

        # Fetch point-in-time metrics (only need one sample)
        memory_data: dict[str, Any] = {}
        swap_data: dict[str, Any] = {}
        gpu_data: list[dict[str, Any]] = []
        temps_data: list[dict[str, Any]] = []
        load_data: dict[str, Any] = {}

        try:
            mem_resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/mem", timeout=5.0
            )
            if mem_resp.status_code == 200:
                memory_data = mem_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch mem: %s", e)

        try:
            swap_resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/memswap", timeout=5.0
            )
            if swap_resp.status_code == 200:
                swap_data = swap_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch memswap: %s", e)

        try:
            gpu_resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/gpu", timeout=5.0
            )
            if gpu_resp.status_code == 200:
                gpu_data = gpu_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch gpu: %s", e)

        try:
            temps_data = (await asyncio.to_thread(get_hw_sensors))["rows"]
        except Exception as e:
            logger.warning("History: failed to read hwmon sensors: %s", e)

        try:
            load_resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/load", timeout=5.0
            )
            if load_resp.status_code == 200:
                load_data = load_resp.json()
        except Exception as e:
            logger.warning("History: failed to fetch load: %s", e)

        # Process list, only when per-process history is on: Glances' point-in-time
        # view (cpu_percent is over its own refresh interval), sanitized below.
        process_data: list[Any] = []
        if processes_enabled:
            try:
                proc_resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/processlist", timeout=10.0
                )
                if proc_resp.status_code == 200:
                    process_data = proc_resp.json()
            except Exception as e:
                logger.warning("History: failed to fetch processlist: %s", e)

        # Fetch filesystem data for drive usage
        fs_data: list[dict[str, Any]] = []
        try:
            fs_resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/fs", timeout=5.0
            )
            if fs_resp.status_code == 200:
                # De-dupe duplicate-device entries (e.g. PrivateTmp /tmp, /var/tmp
                # bind mounts mirroring /) so history drives + per-drive I/O labels
                # match the live view. This fetch bypasses the proxy, so dedupe here.
                fs_data = await asyncio.to_thread(_shape_fs, fs_resp.json(), False)
        except Exception as e:
            logger.warning("History: failed to fetch fs: %s", e)

    # Stamped before the public-IP lookup, which takes seconds when the WAN is
    # down: exactly when the record's place on the time axis matters.
    recorded_at: str = datetime.now(timezone.utc).isoformat()

    # Fetch IP info (point-in-time)
    ip_data: dict[str, Any] = {}
    try:
        if not live:
            ip_data = await _ip_info_cached()
    except Exception as e:
        logger.warning("History: failed to fetch IP info: %s", e)

    # Build the record
    record: dict[str, Any] = {
        "timestamp": recorded_at,
        "duration_sec": sample_duration,
    }

    def _gb(value: object) -> float | None:
        """bytes -> GB, preserving None (unavailable) rather than dividing None."""
        value = _num(value)
        return round(value / (1024**3), 2) if value is not None else None

    # CPU (with min/avg/max)
    if cpu_samples:
        record["cpu"] = compute_stats(cpu_samples)

    # CPU core spread: min/median/max across cores. Distinguishes "one core pinned"
    # from "all cores evenly loaded" — the total-only "cpu" stat cannot. Omitted
    # when /api/4/percpu is never readable.
    cpu_cores: dict[str, float] | None = aggregate_cpu_cores(percpu_samples)
    if cpu_cores is not None:
        record["cpu_cores"] = cpu_cores

    # CPU clocks: min/median/avg/max MHz over every core and sample of the burst,
    # with the hardware range. Omitted on a box without cpufreq.
    if freq_samples:
        cpu_freq: dict[str, float | None] | None = aggregate_cpu_freq(
            freq_samples, await asyncio.to_thread(_cpu_freq_limits)
        )
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
    fs: dict[str, Any]
    for fs in fs_data:
        device_name: str = fs.get("device_name", "")
        mnt_point: str = fs.get("mnt_point", "")
        if device_name and mnt_point:
            resolved: str = os.path.realpath(device_name)
            if resolved.startswith("/dev/"):
                mounted_disk: str = resolved[5:]
                if mounted_disk not in disk_to_mount or len(mnt_point) < len(
                    disk_to_mount[mounted_disk]
                ):
                    disk_to_mount[mounted_disk] = mnt_point
    # The whole-box totals count the MOUNTED devices only: Glances' diskio rows are
    # one per block device including partitions and md/dm layers, so summing every
    # row counts a write on an md mirror three times (member, member, md). Without
    # fs data (Glances could not read the mounts) every row is counted, as before.
    seen_disks: set[str] = {d for it in disk_iter_samples for d in it}
    counted: set[str] = {d for d in seen_disks if d in disk_to_mount} or seen_disks
    # Each device is capped already; the total is their plain sum (a busy pool
    # legitimately exceeds one drive's ceiling).
    disk_read_samples: list[float | None] = [
        _sum_known([v[0] for d, v in it.items() if d in counted])
        for it in disk_iter_samples
    ]
    disk_write_samples: list[float | None] = [
        _sum_known([v[1] for d, v in it.items() if d in counted])
        for it in disk_iter_samples
    ]
    if disk_read_samples or disk_write_samples:
        disk_io: dict[str, Any] = {
            "read_mbps": compute_stats(disk_read_samples),
            "write_mbps": compute_stats(disk_write_samples),
        }
        record["disk_io"] = disk_io

        # Per-drive disk I/O (only for mounted filesystems, like live chart)
        drives_io: list[dict[str, Any]] = []
        for disk_name in sorted(seen_disks & disk_to_mount.keys()):
            samples: list[tuple[float | None, float | None]] = [
                it[disk_name] for it in disk_iter_samples if disk_name in it
            ]
            drives_io.append(
                {
                    "name": disk_to_mount[disk_name],  # Use mount point as name
                    "read_mbps": compute_stats([s[0] for s in samples]),
                    "write_mbps": compute_stats([s[1] for s in samples]),
                }
            )
        if drives_io:
            disk_io["drives"] = drives_io

    # The caps this record was collected under, per interface / per drive. Its
    # presence tells clamp_record_rates the totals are already sane sums (older
    # records only have totals, so reading clamps those instead).
    record["rate_caps"] = {
        "network_mbps": MAX_NETWORK_MBPS,
        "disk_io_mbps": MAX_DISK_IO_MBPS,
    }
    capped: list[dict[str, Any]] = caps.note(disk_to_mount)
    if capped:
        record["rate_capped"] = capped
        if not live:
            logger.warning(
                "Rate cap hit: %s",
                "; ".join(
                    f"{c['metric']} {c.get('interface') or c.get('drive') or c.get('device')} "
                    f"{c['samples']}/{c['of']} samples, raw max {c['raw_max']:g} MB/s (cap {c['cap']:g})"
                    for c in capped
                ),
            )

    # Drive usage (filesystem, point-in-time)
    if fs_data:
        drives: list[dict[str, Any]] = []
        for fs in fs_data:
            mnt_point = fs.get("mnt_point", "")
            if mnt_point:
                drives.append(
                    {
                        "mount": mnt_point,
                        "used_gb": _gb(fs.get("used")),
                        "total_gb": _gb(fs.get("size")),
                        "percent": _round(fs.get("percent"), 1),
                        "inodes_percent": _round(fs.get("inodes_percent"), 1),
                        "inodes_used": fs.get("inodes_used"),
                    }
                )
        if drives:
            record["drives"] = drives

    # GPUs (point-in-time). Sourced from nvidia-smi (Glances reports some datacenter
    # GPUs as all-null); Glances utilization is a fallback. Unreadable fields stay None.
    # Offloaded: get_gpu_stats() shells out to nvidia-smi (up to ~5s), which would
    # otherwise block the event loop for the whole recording cycle.
    gpu_stats: list[dict[str, Any]] = await asyncio.to_thread(get_gpu_stats)
    gpus: list[dict[str, Any]] = build_gpu_records(gpu_stats, gpu_data)
    if gpus:
        record["gpus"] = gpus
    # GPU fans in percent of their maximum (nvidia-smi has no RPM): their own field,
    # so the RPM series in `fans` stay one unit; the Fans chart draws these on a
    # second, 0-100 % axis. A passive card (N/A) has no entry.
    gpu_fans: dict[str, float] = {
        f"GPU {st['index']} fan": st["fan_percent"]
        for st in gpu_stats
        if st.get("fan_percent") is not None
    }
    if gpu_fans:
        record["gpu_fans"] = gpu_fans

    # Temperatures (point-in-time): CPU/board sensors from hwmon (hwmon.py), plus GPU
    # temps from nvidia-smi (merged into the same chart as the live view).
    temps: dict[str, float | None] = {}
    # Power (point-in-time, watts): hwmon's chip readings, the collector's BMC and
    # RAPL rows, and each GPU's draw from nvidia-smi. power_total names the one
    # reading that is the whole machine, so the stacked chart never guesses.
    power: dict[str, float | None] = {}
    power_total: str | None = None
    # Every whole-machine reading, so the stacked chart never stacks one as a part.
    power_totals: list[str] = []
    hw_fans: dict[str, float] = {}
    for sensor in temps_data:
        value_hw: float | None = _round(sensor.get("value"), 1)
        if sensor.get("type") == hwmon.FAN and value_hw is not None:
            hw_fans[sensor.get("id") or sensor["label"]] = value_hw
        elif sensor.get("type") == hwmon.POWER and value_hw is not None:
            power[sensor["label"]] = value_hw
            if sensor.get("total"):
                power_totals.append(sensor["label"])
                if power_total is None:
                    power_total = sensor["label"]
    sensor: dict[str, Any]
    for sensor in temps_data:
        # Only include temperature sensors (exclude battery, fan speed, etc.)
        if sensor.get("type") != "temperature_core":
            continue
        label: str = sensor.get("label", "")
        value: float | None = _round(sensor.get("value"), 1)
        if label and value is not None:
            # Use label as key, handling duplicates by keeping max
            previous: float | None = temps.get(label)
            temps[label] = max(previous, value) if previous is not None else value
    temps.update(gpu_temps(gpu_stats, gpu_data))
    # Chassis sensors from the collector's 60 s sensors snapshot (IPMI and RAPL are
    # root-only, so the OS cannot see them). Only a fresh snapshot counts: a dead
    # collector then leaves gaps in the charts instead of a flat line forever.
    sensors: dict[str, Any] = await asyncio.to_thread(
        _get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC
    )
    if sensors["status"] == "ok":
        temps.update(
            {
                r["label"]: r["value"]
                for r in sensors.get("temps") or []
                if r.get("value") is not None
            }
        )
        hw_fans.update(
            {
                r["label"]: r["value"]
                for r in sensors.get("fans") or []
                if r.get("value") is not None
            }
        )
        row: dict[str, Any]
        bmc_total: bool = False
        for row in collector_power_rows(sensors):
            power[row["label"]] = row["value"]
            # The BMC measures the whole box: it wins over an hwmon meter. The
            # first BMC total, as the live power card (app.js) picks it.
            if row["total"]:
                power_totals.append(row["label"])
                if not bmc_total:
                    power_total, bmc_total = row["label"], True
    # Drive temperatures from the 15-min snapshot (SMART), fresh only, keyed by device.
    health: dict[str, Any] = await asyncio.to_thread(_get_storage_health)
    if health["status"] == "ok":
        temps.update(
            {
                d["device"]: d["temperature_c"]
                for d in health.get("drives") or []
                if isinstance(d, dict)
                and d.get("device")
                and d.get("temperature_c") is not None
            }
        )
    if hw_fans:
        record["fans"] = hw_fans
    if temps:
        record["temps"] = temps
    st: dict[str, Any]
    for st in gpu_stats:
        if st.get("index") is not None and st.get("power_draw") is not None:
            power[f"GPU {st['index']}"] = _round(st["power_draw"], 1)
    if power:
        record["power"] = power
        if power_total is not None:
            record["power_total"] = power_total
            if len(power_totals) > 1:
                record["power_totals"] = power_totals

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
    if processes_enabled:
        gpu_procs: list[dict[str, Any]] = []
        try:
            gpu_procs = await asyncio.to_thread(_get_gpu_processes)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("History: failed to read GPU processes: %s", e)
        try:
            record["processes"] = await asyncio.to_thread(
                build_process_records,
                process_data if isinstance(process_data, list) else [],
                gpu_procs,
                process_count,
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


_history_write_lock: _thread.LockType = threading.Lock()


def append_history_record(record: dict[str, Any]) -> None:
    """Append a history record to the JSONL file.

    The scheduled recorder and a manual /api/history/record both call this from
    worker threads; the lock keeps two records from interleaving into one torn
    line (a record over the 8 KiB text buffer is more than one write()).
    """
    _append_jsonl(HISTORY_FILE, record)
    logger.info("History record saved: %s", record.get("timestamp"))


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one record as a line of ``path`` (history.jsonl, services.jsonl)."""
    line: bytes = json.dumps(record).encode("utf-8") + b"\n"
    LOGS_DIR.mkdir(exist_ok=True)
    f: BinaryIO
    with _history_write_lock, open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            tail: bytes = f.read(1)
            if not tail:
                raise OSError("Cannot read history tail")
            # Preserve a failed append's bytes, even an incomplete UTF-8 code
            # point. Separate them so the next valid record stays readable.
            if tail != b"\n":
                line = b"\n" + line
        f.seek(0, os.SEEK_END)
        f.write(line)
        f.flush()


def missing_core_metrics(record: dict[str, Any]) -> list[str]:
    """Core metric groups that are absent, or present but entirely null.

    Presence of the key is not evidence the sample worked: a Glances brownout
    returning null totals still yields ``cpu_samples == [None, ...]``, which is a
    truthy list, so ``record["cpu"]`` gets written as ``{"avg": None, ...}``.
    Callers use this to avoid reporting a near-empty record as a healthy one.
    """
    missing: list[str] = []
    cpu: Any = record.get("cpu")
    if not isinstance(cpu, dict) or cpu.get("avg") is None:
        missing.append("cpu")
    memory: Any = record.get("memory")
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
    missing: list[str] = missing_core_metrics(record)
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
        e: Exception
        try:
            glances_url: str = APP.state.glances_url
            record: dict[str, Any] = await collect_history_sample(glances_url)
            await asyncio.to_thread(append_history_record, record)
            _apply_record_health(record)
        except Exception as e:
            # Surfaced to the frontend via /api/history's "recording" block so a
            # stalled recorder is visible instead of silently rotting for days.
            APP.state.history_last_error = str(e)
            logger.error("History recording failed: %s", e)

        await asyncio.sleep(APP.state.history_record_interval)


def _read_service_rounds(cutoff: float) -> tuple[list[ServiceRound], bool]:
    """The rounds in services.jsonl from ``cutoff`` on (sync), and whether the
    read stopped at the byte cap before reaching it. No file yet is no rounds;
    raises OSError when the file cannot be read."""
    if not SERVICES_FILE.exists():
        return [], False
    window: _HistoryWindow = _read_history_window(cutoff, None, path=SERVICES_FILE)
    rounds: list[ServiceRound] = []
    ts: float
    record: dict[str, Any]
    for ts, record in window.records:
        parsed: ServiceRound | None = _service_round(ts, record)
        if parsed is not None and ts >= cutoff:
            rounds.append(parsed)
    return rounds, window.hit_cap and not _history_window_covers(window, cutoff, None)


async def run_service_round(
    client: httpx.AsyncClient, insecure_client: httpx.AsyncClient
) -> dict[str, Any]:
    """Check every target once, append the round to services.jsonl and add it
    to the in-memory window. Returns the record written."""
    now: datetime = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "interval": SERVICES_CHECK_INTERVAL,
        "targets": await check_http_targets(
            HTTP_TARGETS,
            client,
            insecure_client,
            SERVICES_TIMEOUT,
            SERVICES_RETRY_DELAY,
        ),
    }
    # In memory first: a full disk must not blank the card's current state.
    _note_service_round(record, now.timestamp())
    await asyncio.to_thread(_append_jsonl, SERVICES_FILE, record)
    return record


async def services_checker() -> None:
    """Background task: a round of service checks every services.check_interval
    seconds, the first one at once. Started only when http_targets is not empty."""
    global _services_started
    _services_started = time.time()
    e: Exception
    try:
        cutoff: float = time.time() - SERVICES_UPTIME_HOURS[-1] * 3600
        loaded: list[ServiceRound] = (
            await asyncio.to_thread(_read_service_rounds, cutoff)
        )[0]
        _services_rounds.clear()
        _services_rounds.extend(loaded)
    except Exception as e:
        # Uptime then counts from this start; the checks themselves still run.
        logger.error("Service checks: cannot read %s: %s", SERVICES_FILE, e)
    limits: httpx.Limits = httpx.Limits(max_keepalive_connections=0)
    client: httpx.AsyncClient
    insecure_client: httpx.AsyncClient
    # Their own clients, so a slow target never queues behind the page's Glances
    # requests; no keep-alive, so every round proves a new connection is accepted.
    async with (
        httpx.AsyncClient(follow_redirects=True, limits=limits) as client,
        httpx.AsyncClient(
            follow_redirects=True, limits=limits, verify=False
        ) as insecure_client,
    ):
        while True:
            started: float = time.monotonic()
            try:
                await run_service_round(client, insecure_client)
            except Exception:
                # An unhandled exception would end this task and every later check.
                logger.exception("service check round failed")
            await asyncio.sleep(
                max(1.0, SERVICES_CHECK_INTERVAL - (time.monotonic() - started))
            )


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
    deadline: float = time.monotonic() + timeout
    last: float | None = None
    changes: int = 0
    client: httpx.AsyncClient
    async with _glances_client(recording=True) as client:
        while time.monotonic() < deadline:
            try:
                resp: httpx.Response = await client.get(
                    f"{glances_url}/api/4/cpu", timeout=5.0
                )
                total: float | None = (
                    _num(resp.json().get("total")) if resp.status_code == 200 else None
                )
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
    e: Exception
    try:
        if not await wait_for_glances_warm(glances_url):
            logger.warning(
                "Glances CPU readings still cold after 20 s; recording anyway"
            )
        logger.info("Recording initial history sample")
        initial_sample: dict[str, Any] = await collect_history_sample(glances_url)
        await asyncio.to_thread(append_history_record, initial_sample)
        _apply_record_health(initial_sample)
    except Exception as e:
        APP.state.history_last_error = str(e)
        logger.warning("Failed to record startup history: %s", e)


GLANCES_MAX_MISSES: Final[int] = (
    3  # failed 5 s probes in a row (~30 s) before a restart
)


async def _glances_answers() -> bool:
    try:
        # Its own client: in the shared pool the probe queues behind the page's
        # requests, and a pool timeout would count as a dead Glances.
        # verify=False: plain HTTP on localhost, and building the TLS context
        # costs ~10 ms on the event loop at every probe.
        client: httpx.AsyncClient
        async with httpx.AsyncClient(verify=False) as client:
            resp: httpx.Response = await client.get(
                f"{APP.state.glances_url}/api/4/cpu", timeout=5.0
            )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def check_disconnected_clients() -> None:
    """Background task (every 5 s): restart Glances if it died or stopped
    answering, and log clients inactive for 30+ seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            # systemd only restarts the unit when THIS process exits; a dead or
            # hung Glances (stuck on a dead mount, a sensor read) would otherwise
            # leave every card 502 and the history partial for good. At most one
            # restart a minute: a Glances that cannot start at all must not be
            # respawned every 5 s forever.
            glances: subprocess.Popen[bytes] = APP.state.glances_process
            why: str | None = None
            if glances.poll() is not None:
                why = f"exited with code {glances.returncode}"
            elif await _glances_answers():
                APP.state.glances_misses = 0
            else:
                APP.state.glances_misses = getattr(APP.state, "glances_misses", 0) + 1
                if APP.state.glances_misses >= GLANCES_MAX_MISSES:
                    why = f"has not answered for {APP.state.glances_misses} checks"
            if why and time.time() - APP.state.glances_started_at >= 60:
                logger.error("Glances %s; restarting (see logs/glances.log)", why)
                if glances.poll() is None:
                    glances.kill()
                    await asyncio.to_thread(glances.wait, 5)
                APP.state.glances_process = start_glances(APP.state.glances_port)
                APP.state.glances_started_at = time.time()
                APP.state.glances_misses = 0
            now: float = time.time()
            disconnected: list[str] = [
                ip for ip, ts in client_last_seen.items() if now - ts > CLIENT_TIMEOUT
            ]
            ip: str
            for ip in disconnected:
                del client_last_seen[ip]
                logger.info("Client disconnected: %s", ip)
        except Exception:
            # An unhandled exception would end this task silently, and with it
            # every future Glances restart. Log it and keep ticking.
            logger.exception("watchdog tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage Glances subprocess lifecycle and background tasks."""
    glances_port: int = app.state.glances_port

    # Startup
    # Enforce 0600 on the password hash even if it was created/edited another way
    # (a group- or world-readable .password leaks the PBKDF2 hash for offline attack).
    # Only when loose: the systemd unit mounts the checkout read-only, so the
    # chmod fails there and should warn only when the file actually needs it.
    if PASSWORD_FILE.exists() and PASSWORD_FILE.stat().st_mode & 0o077:
        e: OSError
        try:
            PASSWORD_FILE.chmod(0o600)
        except OSError as e:
            logger.warning("Could not tighten permissions on %s: %s", PASSWORD_FILE, e)

    async with _glances_clients():
        tasks: list[asyncio.Task[None]] = []
        process_started: bool = False
        app.state.history_task = None
        try:
            logger.info("Starting Glances web server on port %d...", glances_port)
            app.state.glances_process = start_glances(glances_port)
            process_started = True
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
            disconnect_task: asyncio.Task[None] = asyncio.create_task(
                check_disconnected_clients()
            )

            tasks.append(disconnect_task)

            # Start background task for history recording
            app.state.history_task = asyncio.create_task(history_recorder())
            tasks.append(app.state.history_task)
            logger.info(
                "History recording enabled (every %d seconds)",
                DEFAULT_HISTORY_RECORD_INTERVAL,
            )

            # Always on: the server's own fan rules need no collector to be installed.
            alarm_task: asyncio.Task[None] = asyncio.create_task(alarm_tracker())

            tasks.append(alarm_task)

            services_task: asyncio.Task[None] | None = None
            if HTTP_TARGETS:
                services_task = asyncio.create_task(services_checker())
                tasks.append(services_task)
                logger.info(
                    "Service checks enabled (%d targets every %d seconds)",
                    len(HTTP_TARGETS),
                    SERVICES_CHECK_INTERVAL,
                )

            # Give Glances time to start
            await asyncio.sleep(2)

            # Record initial sample in background (non-blocking, last step of startup)
            initial_sample_task: asyncio.Task[None] = asyncio.create_task(
                record_initial_sample(app.state.glances_url)
            )

            tasks.append(initial_sample_task)

            yield  # Server runs here
        finally:
            # Interval changes can replace the recorder while the app is running.
            # Drain the current task as well as any task started before a failure.
            if (
                app.state.history_task is not None
                and app.state.history_task not in tasks
            ):
                tasks.append(app.state.history_task)
            task: asyncio.Task[None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process_started:
                # The watchdog may have replaced the original subprocess.
                stop_glances(app.state.glances_process)


# No /docs, /redoc, /openapi.json: nothing uses them and the service is unauthenticated.
APP: FastAPI = FastAPI(
    lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)


class _GZipUnlessRange:
    """GZipMiddleware, except for requests with a Range header: starlette would
    gzip a 206 body while Content-Range still counts uncompressed bytes, so a
    resumed download (curl -C - of /api/history/raw) would splice gzip into the
    file."""

    def __init__(
        self: _GZipUnlessRange,
        app: ASGIApp,
        minimum_size: int = 500,
        compresslevel: int = 9,
    ) -> None:
        self.app: ASGIApp = app
        self.gzip: GZipMiddleware = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel
        )

    async def __call__(
        self: _GZipUnlessRange, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http" and any(k == b"range" for k, _ in scope["headers"]):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


# History JSON shrinks 5-10x gzipped; a 30-day window is ~6 MB raw. Only clients
# that send Accept-Encoding: gzip get it (browsers, httpx), so tui.py's urllib
# keeps reading /text uncompressed. Proxied Glances bodies arrive decoded with
# content-encoding stripped (HOP_BY_HOP_HEADERS), so recompressing them is safe.
# Level 1: compression runs on the event loop, and on JSON it gets most of
# level 6's ratio for a fraction of the time.
APP.add_middleware(_GZipUnlessRange, minimum_size=1000, compresslevel=1)


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


class _RevalidatedStaticFiles(StaticFiles):
    """StaticFiles with Cache-Control: no-cache. Without it browsers cache the JS
    and CSS by heuristic, so an update keeps running the old frontend; with it
    each load revalidates, and an unchanged file is a 304."""

    def file_response(
        self: _RevalidatedStaticFiles,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response: Response = super().file_response(
            full_path, stat_result, scope, status_code
        )
        response.headers["Cache-Control"] = "no-cache"
        return response


APP.mount("/static", _RevalidatedStaticFiles(directory=STATIC_DIR), name="static")


@APP.middleware("http")
async def log_connections(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
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


def _clamp_rate(value: float, cap: float) -> float:
    """A rate in MB/s bounded to [0, cap]."""
    return min(max(value, 0.0), cap)


class _RateCaps:
    """Caps one record's rate samples per interface / per drive and remembers
    what went over, for the record's ``rate_capped`` note.

    A series is (metric, interface or disk name). ``sample`` takes Glances'
    bytes/s and returns MB/s. A sample over the cap counts as the cap; it is
    never dropped, since that would hide real traffic if the cap is set too low.
    A missing (None), non-finite or non-numeric rate is not a measurement: it
    is returned as 0 for the sums and not counted, so it can neither crash the
    record nor become a cap-height spike.
    """

    def __init__(self: _RateCaps) -> None:
        self.seen: dict[tuple[str, str], int] = {}
        self.hits: dict[tuple[str, str], list[float]] = {}

    def sample(
        self: _RateCaps, bytes_per_sec: object, cap: float, series: tuple[str, str]
    ) -> float | None:
        n: float | None = _num(bytes_per_sec)
        if n is None:
            return None  # Glances has no rate yet (its first reading, a new device)
        n /= 1e6
        self.seen[series] = self.seen.get(series, 0) + 1
        if n > cap:
            self.hits.setdefault(series, []).append(n)
        return _clamp_rate(n, cap)

    def note(self: _RateCaps, disk_to_mount: dict[str, str]) -> list[dict[str, Any]]:
        """``[{metric, interface | device [+ drive], samples, of, raw_max, cap}]``,
        one entry per series that went over its cap. ``samples`` of ``of`` counts
        that interface's or device's own samples in the burst; ``drive`` is the
        mount point when the device is mounted (as in disk_io.drives)."""
        note: list[dict[str, Any]] = []
        metric: str
        name: str
        raw: list[float]
        for (metric, name), raw in sorted(self.hits.items()):
            entry: dict[str, Any] = {"metric": metric}
            if metric.startswith("network."):
                entry["interface"] = name
                cap: float = MAX_NETWORK_MBPS
            else:
                entry["device"] = name
                if name in disk_to_mount:
                    entry["drive"] = disk_to_mount[name]
                cap = MAX_DISK_IO_MBPS
            entry.update(
                samples=len(raw),
                of=self.seen.get((metric, name), len(raw)),
                raw_max=_round(max(raw), 2),
                cap=cap,
            )
            note.append(entry)
        return note


def _clamp_stats(stats: Any, cap: float) -> None:
    """Clamp the avg/min/max of one compute_stats block in place; None (an
    empty window) and anything that is not a number are left alone."""
    if not isinstance(stats, dict):
        return
    key: Any
    value: Any
    for key, value in stats.items():
        n: float | None = _num(value)
        if n is not None:
            stats[key] = _clamp_rate(n, cap)


def clamp_record_rates(record: dict[str, Any]) -> None:
    """Clamp a served record's rates to the configured limits, in place, if it
    predates the caps: network.in_mbps/out_mbps, disk_io.read_mbps/write_mbps
    and every disk_io.drives[] entry. A record with ``rate_caps`` was capped per
    interface / per drive when taken and is served as recorded. The file is
    never rewritten.
    """
    if "rate_caps" in record:
        return
    net: Any = record.get("network")
    if isinstance(net, dict):
        key: str
        for key in ("in_mbps", "out_mbps"):
            _clamp_stats(net.get(key), MAX_NETWORK_MBPS)
    dio: Any = record.get("disk_io")
    if isinstance(dio, dict):
        for key in ("read_mbps", "write_mbps"):
            _clamp_stats(dio.get(key), MAX_DISK_IO_MBPS)
        drives: Any = dio.get("drives")
        if isinstance(drives, list):
            drive: Any
            for drive in drives:
                if isinstance(drive, dict):
                    for key in ("read_mbps", "write_mbps"):
                        _clamp_stats(drive.get(key), MAX_DISK_IO_MBPS)


class _HistoryWindow(NamedTuple):
    """The parsed tail of the history file: what one read of it found."""

    stat: tuple[int, int]  # the file's (st_mtime_ns, st_size) when it was read
    size: int  # bytes of the file the window spans
    records: list[tuple[float, dict[str, Any]]]  # (epoch, record), in file order
    skipped: int  # non-empty lines that are not a record
    whole_file: bool  # the window starts at the file's first byte
    hit_cap: bool  # the byte cap stopped it from growing further


def _history_window_covers(
    window: _HistoryWindow, cutoff: float, since: float | None
) -> bool:
    """Whether the window holds every record a (cutoff, since) request can ask for:
    it starts at the file's start, or with a record older than the cutoff, or (with
    ``since``) with one at or before ``since``."""
    if window.whole_file:
        return True
    if not window.records:
        return False
    first_ts: float = window.records[0][0]
    return first_ts < cutoff or (since is not None and first_ts <= since)


def _parse_history_lines(data: bytes) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """Whole JSONL lines as (epoch, record) pairs with served rates clamped, and
    the count of non-empty lines that are not a record with a valid timestamp."""
    records: list[tuple[float, dict[str, Any]]] = []
    skipped: int = 0
    line: bytes
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record: dict[str, Any] = json.loads(line)
            ts_str: str = (
                record.get("timestamp", "") if isinstance(record, dict) else ""
            )
            # Handle both formats: with and without Z suffix
            # (empty/missing/non-string timestamp raises ValueError -> skipped)
            if not isinstance(ts_str, str):
                raise ValueError("timestamp is not a string")
            ts_str = ts_str.replace("Z", "+00:00")
            ts: float = datetime.fromisoformat(ts_str).timestamp()
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        clamp_record_rates(record)
        records.append((ts, record))
    return records, skipped


def _read_history_window(
    cutoff: float,
    since: float | None,
    initial_read_size: int = HISTORY_INITIAL_TAIL_READ,
    max_bytes: int = HISTORY_MAX_TAIL_READ,
    path: Path | None = None,
) -> _HistoryWindow:
    """Parse the tail of the history file (or ``path``, another JSONL file of
    timestamped records), growing the window (doubling) until it covers
    (cutoff, since) or reaches ``max_bytes``. Each doubling reads and
    parses only the part of the file the window did not hold yet. Raises OSError
    when the file cannot be read."""
    f: BinaryIO
    with open(path or HISTORY_FILE, "rb") as f:
        st: os.stat_result = os.fstat(f.fileno())
        size: int = st.st_size
        records: list[tuple[float, dict[str, Any]]] = []
        skipped: int = 0
        # Bytes from here to the end are parsed (it is the start of a line); None
        # until a read finds a line boundary to start from.
        parsed_from: int | None = None
        read_size: int = initial_read_size
        while True:
            capped: int = min(read_size, max_bytes)
            offset: int = max(0, size - capped)
            f.seek(offset)
            data: bytes = f.read(
                (size if parsed_from is None else parsed_from) - offset
            )
            if parsed_from is None:
                # An unterminated last line is a record still being appended,
                # not corruption: leave it for the next read.
                data = data[: data.rfind(b"\n") + 1]
            start: int | None = offset
            if offset > 0:
                # Drop the (likely partial) line the window starts in
                nl: int = data.find(b"\n")
                start = offset + nl + 1 if nl != -1 else None
                data = data[nl + 1 :] if nl != -1 else b""
            if start is not None:
                new_records: list[tuple[float, dict[str, Any]]]
                new_skipped: int
                new_records, new_skipped = _parse_history_lines(data)
                records = new_records + records
                skipped += new_skipped
                parsed_from = start
            window: _HistoryWindow = _HistoryWindow(
                stat=(st.st_mtime_ns, size),
                size=size - offset,
                records=records,
                skipped=skipped,
                whole_file=offset == 0,
                # offset > 0 with a maxed window means we can't grow further.
                hit_cap=offset > 0 and capped >= max_bytes,
            )
            if window.hit_cap or _history_window_covers(window, cutoff, since):
                if skipped:
                    logger.warning("History read: skipped %d malformed lines", skipped)
                if window.hit_cap and not _history_window_covers(window, cutoff, since):
                    logger.warning(
                        "History read: hit %d-byte cap before covering the range; "
                        "the window holds the most recent %d records",
                        max_bytes,
                        len(records),
                    )
                return window
            read_size *= 2


def _serve_history_window(
    window: _HistoryWindow, cutoff: float, since: float | None
) -> tuple[list[dict[str, Any]], int, bool, float]:
    """A request's (records, skipped, truncated, cutoff) from a window: the
    records at or after the cutoff and (with ``since``) strictly newer than it.
    The record dicts are the window's own: callers must not change them."""
    records: list[dict[str, Any]] = [
        record
        for ts, record in window.records
        if ts >= cutoff and (since is None or ts > since)
    ]
    truncated: bool = window.hit_cap and not _history_window_covers(
        window, cutoff, since
    )
    return records, window.skipped, truncated, cutoff


def _read_history(
    hours: float,
    initial_read_size: int = HISTORY_INITIAL_TAIL_READ,
    max_bytes: int = HISTORY_MAX_TAIL_READ,
    since: float | None = None,
) -> tuple[list[dict[str, Any]], int, bool, float] | str:
    """Read history records from the tail of the JSONL file (sync, called from thread pool).

    Records are appended chronologically, so instead of scanning the whole file
    we read a window from the end and grow it (doubling) until the oldest record
    in the window predates the cutoff — i.e. the window covers the requested range.
    The window never exceeds ``max_bytes``: a range whose cutoff is older than the
    oldest record in that window (e.g. "1 year" on a partial file) would otherwise
    read the whole — potentially huge — file, so we stop at the cap and report it.

    ``since`` (epoch seconds) is the incremental path: only records strictly newer
    than it are returned, and the window is covered as soon as it reaches a record
    at or before ``since`` — a client topping up a recent view parses one small
    window instead of the whole range. ``truncated`` then means the byte cap was
    hit before reaching ``since``: there is a gap between the client's newest
    record and the oldest one returned, and the client should refetch in full.
    The filter is by timestamp, so a client that wants records appended out of
    timestamp order (a clock step, two recorders finishing in reverse) passes a
    ``since`` somewhat before its newest record and drops the duplicates.

    Returns (records, skipped_line_count, truncated, cutoff) on success, or an
    error string on failure. ``cutoff`` is the window start as epoch seconds (the
    client trims its cached records against it). ``truncated`` is True when the
    byte cap was hit before the range was covered (the oldest returned data is
    newer than the requested cutoff). Skipped lines are non-empty lines that
    don't parse as a dict with a valid timestamp — corruption worth surfacing,
    not silently dropping. Served rates are clamped (clamp_record_rates).
    """
    cutoff: float = datetime.now(timezone.utc).timestamp() - (hours * 3600)

    if not HISTORY_FILE.exists():
        return [], 0, False, cutoff

    e: Exception
    try:
        window: _HistoryWindow = _read_history_window(
            cutoff, since, initial_read_size, max_bytes
        )
    except Exception as e:
        logger.error("Failed to read history file: %s", e)
        return str(e)
    return _serve_history_window(window, cutoff, since)


def _recording_health() -> dict[str, Any]:
    """Recorder liveness for the frontend: last success, last error, and whether
    it looks stalled — no successful record within ~2 record intervals plus one
    sample window (comfortably longer than a normal gap, so it doesn't false-fire
    every cycle). This is what turns a silently-dead recorder into a visible one.
    """
    last_success: str | None = getattr(APP.state, "history_last_success", None)
    interval: float = getattr(
        APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL
    )
    duration: float = getattr(
        APP.state, "history_sample_duration", DEFAULT_HISTORY_SAMPLE_DURATION
    )
    stale: bool = False
    # No success since start: age from the start time, so a recorder that has
    # never worked (Glances dead from boot) still trips stale on schedule.
    since: str | None = last_success or getattr(APP.state, "started_at", None)
    if since:
        try:
            age: float = (
                datetime.now(timezone.utc).timestamp()
                - datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
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


def _parse_since(since: str | None) -> float | None | Response:
    """The ``since`` query parameter (any ISO 8601 timestamp; the browser sends
    the previous response's ``now`` minus a few minutes) as epoch seconds; None
    when absent; a 400 Response when it does not parse."""
    if since is None or since == "":
        return None
    # An unencoded "+00:00" pasted into a URL arrives as " 00:00"; put the plus
    # back on the offset only (a space between date and time is valid ISO).
    text: str = re.sub(r"(:\d\d(?:\.\d+)?) (\d\d:\d\d)$", r"\1+\2", since.strip())
    text = text.replace("Z", "+00:00")
    try:
        dt: datetime = datetime.fromisoformat(text)
    except ValueError:
        return Response(
            content=json.dumps({"error": "since must be an ISO 8601 timestamp"}),
            status_code=400,
            media_type="application/json",
        )
    # Records are UTC; a timestamp without an offset is read as UTC too, not as
    # the server's local time.
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _clamp_hours(hours: float) -> float:
    """A requested history window, clamped to [0.1, history.max_hours]: too large
    a window is served as the largest allowed one, not refused."""
    return max(0.1, min(hours, HISTORY_MAX_HOURS))


@APP.get("/api/history")
async def get_history(hours: float = 24, since: str | None = None) -> Response:
    """Return historical data for the specified time range.

    Args:
        hours: Number of hours of history to return (default 24, max
            history.max_hours, 8760 unless set).
        since: Only records strictly newer than this timestamp are returned
            (the client merges them and trims against ``cutoff``). Responses
            carry ``now`` (the server's clock) for the client's next since.
    """
    hours = _clamp_hours(hours)
    since_ts: float | None | Response = _parse_since(since)
    if isinstance(since_ts, Response):
        return since_ts
    result: tuple[list[dict[str, Any]], int, bool, float] | str = await _history_result(
        hours, since_ts
    )

    if isinstance(result, str):
        return Response(
            content=json.dumps({"error": "Failed to read history", "detail": result}),
            status_code=500,
            media_type="application/json",
        )

    records: list[dict[str, Any]]
    skipped: int
    truncated: bool
    cutoff: float
    records, skipped, truncated, cutoff = result
    # Records embed an `ip` block (LAN/public/Tailscale). Nothing in the UI reads it
    # from history — the IP-Info card uses live /api/ip-info — so don't serve a
    # network-topology history to unauthenticated LAN clients. The password-gated
    # /api/history/raw still exposes it for deliberate export.
    # The heavy per-record fields are served by /api/history/field/<name> on demand.
    # The records are shared with other requests (the parsed-history cache), so
    # the fields are left out of copies, never popped.
    left_out: frozenset[str] = frozenset({"ip", *HISTORY_SIDE_FIELDS})
    recording: dict[str, Any] = _recording_health()

    def encode() -> str:
        return json.dumps(
            {
                "records": [
                    {key: value for key, value in rec.items() if key not in left_out}
                    for rec in records
                ],
                "count": len(records),
                "skipped_lines": skipped,
                "truncated": truncated,
                "cutoff": cutoff,
                "now": cutoff + hours * 3600,
                "recording": recording,
            }
        )

    # A long range is many MB of JSON: build and encode it off the event loop.
    return Response(
        content=await asyncio.to_thread(encode), media_type="application/json"
    )


@APP.get("/api/history/field/{name}")
async def get_history_field(
    name: str, hours: float = 24, since: str | None = None
) -> Response:
    """One heavy record field over the range: ``[{timestamp, <name>}, ...]``.

    Only the names in HISTORY_SIDE_FIELDS (the ones /api/history leaves out) are
    served; a record without the field keeps its timestamp so the chart's x-axis
    matches /api/history and the missing field draws as a gap. ``since`` works
    as in /api/history.
    """
    if name not in HISTORY_SIDE_FIELDS:
        return Response(
            content=json.dumps(
                {"error": f"unknown field; one of {sorted(HISTORY_SIDE_FIELDS)}"}
            ),
            status_code=404,
            media_type="application/json",
        )
    hours = _clamp_hours(hours)
    since_ts: float | None | Response = _parse_since(since)
    if isinstance(since_ts, Response):
        return since_ts
    result: tuple[list[dict[str, Any]], int, bool, float] | str = await _history_result(
        hours, since_ts
    )
    if isinstance(result, str):
        return Response(
            content=json.dumps({"error": "Failed to read history", "detail": result}),
            status_code=500,
            media_type="application/json",
        )
    records: list[dict[str, Any]]
    skipped: int
    truncated: bool
    cutoff: float
    records, skipped, truncated, cutoff = result
    slim: list[dict[str, Any]] = [
        {
            "timestamp": rec.get("timestamp"),
            **({name: rec[name]} if name in rec else {}),
        }
        for rec in records
    ]
    body: dict[str, Any] = {
        "field": name,
        "records": slim,
        "count": len(slim),
        "skipped_lines": skipped,
        "truncated": truncated,
        "cutoff": cutoff,
        "now": cutoff + hours * 3600,
    }
    return Response(
        content=await asyncio.to_thread(json.dumps, body), media_type="application/json"
    )


def _history_export_stat() -> os.stat_result:
    # Wait for an in-progress append to finish, then release the writer lock
    # before sending anything. A slow download must never stall recording.
    with _history_write_lock:
        return HISTORY_FILE.stat()


class _HistoryFileResponse(FileResponse):
    """Keep FileResponse's range support, but bound full exports to their size."""

    async def _handle_simple(
        self: _HistoryFileResponse,
        send: Send,
        send_header_only: bool,
        send_pathsend: bool,
    ) -> None:
        remaining: int = int(self.headers["content-length"])
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if send_header_only or not remaining:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        # FileResponse normally reads to EOF (or delegates to pathsend).
        # History keeps growing: read only the bytes advertised at startup.
        file: anyio.AsyncFile[bytes]
        async with await anyio.open_file(self.path, mode="rb") as file:
            while remaining:
                chunk: bytes = await file.read(min(self.chunk_size, remaining))
                if not chunk:
                    raise OSError("History file shrank during export")
                remaining -= len(chunk)
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": remaining > 0,
                    }
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
    denied: Response | None = await _verify_password_throttled(request, password)
    if denied is not None:
        return denied

    try:
        stat_result: os.stat_result = await asyncio.to_thread(_history_export_stat)
    except FileNotFoundError:
        return Response(
            content=json.dumps({"error": "No history file"}),
            status_code=404,
            media_type="application/json",
        )
    return _HistoryFileResponse(
        HISTORY_FILE,
        media_type="application/x-ndjson",
        filename="history.jsonl",
        stat_result=stat_result,
    )


@APP.post("/api/history/record")
async def trigger_history_record() -> Response:
    """Manually trigger a history recording (at most one per
    history.min_record_interval seconds; 0 turns the limit off)."""
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

    e: Exception
    try:
        _last_manual_record_time = now
        glances_url: str = APP.state.glances_url
        record: dict[str, Any] = await collect_history_sample(glances_url)
        await asyncio.to_thread(append_history_record, record)
        _apply_record_health(record)
        # Echo without `ip` (see get_history). Build a new dict rather than popping:
        # `record` is the object that was just written and must stay intact.
        echoed: dict[str, Any] = {
            key: value for key, value in record.items() if key != "ip"
        }
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


_history_settings_lock: asyncio.Lock = asyncio.Lock()


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
        body: dict[str, Any] | None = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return Response(
            content=json.dumps({"error": "Invalid JSON: expected an object"}),
            status_code=400,
            media_type="application/json",
        )

    password: Any = body.get("password", "")
    if not isinstance(password, str):
        # Before the throttle: a malformed body is not a guess, and the KDF's
        # .encode() would turn it into a 500 that still counted as a failure.
        return Response(
            content=json.dumps({"error": "password must be a string"}),
            status_code=400,
            media_type="application/json",
        )
    denied: Response | None = await _verify_password_throttled(request, password)
    if denied is not None:
        return denied

    # Validate new values
    record_interval: Any = body.get("record_interval")
    sample_duration: Any = body.get("sample_duration")
    sample_count: Any = body.get("sample_count")
    min_record_interval: Any = body.get("min_record_interval")
    process_count: Any = body.get("process_count")
    errors: list[str] = []

    if record_interval is not None:
        try:
            record_interval = int(record_interval)
            if not (10 <= record_interval <= 86400):
                errors.append("record_interval must be between 10 and 86400 seconds")
        except (ValueError, TypeError, OverflowError):
            errors.append("record_interval must be an integer")

    if sample_duration is not None:
        try:
            sample_duration = int(sample_duration)
            if not (1 <= sample_duration <= 300):
                errors.append("sample_duration must be between 1 and 300 seconds")
        except (ValueError, TypeError, OverflowError):
            errors.append("sample_duration must be an integer")

    if sample_count is not None:
        try:
            sample_count = int(sample_count)
            if not (1 <= sample_count <= 100):
                errors.append("sample_count must be between 1 and 100")
        except (ValueError, TypeError, OverflowError):
            errors.append("sample_count must be an integer")

    if min_record_interval is not None:
        try:
            min_record_interval = int(min_record_interval)
            if not (0 <= min_record_interval <= 3600):
                errors.append("min_record_interval must be between 0 and 3600 seconds")
        except (ValueError, TypeError, OverflowError):
            errors.append("min_record_interval must be an integer")

    if process_count is not None:
        try:
            process_count = int(process_count)
            if not (1 <= process_count <= 50):
                errors.append("process_count must be between 1 and 50")
        except (ValueError, TypeError, OverflowError):
            errors.append("process_count must be an integer")

    flag: str
    for flag in ("ping_enabled", "processes_enabled"):
        # Anything but a JSON boolean used to read as "off".
        if body.get(flag) is not None and not isinstance(body[flag], bool):
            errors.append(f"{flag} must be true or false")

    if errors:
        return Response(
            content=json.dumps({"error": "Validation failed", "details": errors}),
            status_code=422,
            media_type="application/json",
        )

    async with _history_settings_lock:
        # Apply changes
        changed: bool = False
        interval_changed: bool = (
            record_interval is not None
            and record_interval != APP.state.history_record_interval
        )
        if interval_changed:
            APP.state.history_record_interval = record_interval
            changed = True
        if (
            sample_duration is not None
            and sample_duration != APP.state.history_sample_duration
        ):
            APP.state.history_sample_duration = sample_duration
            changed = True
        if sample_count is not None and sample_count != APP.state.history_sample_count:
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

        ping_enabled: Any = body.get("ping_enabled")
        if ping_enabled is not None and APP.state.ping_enabled != (
            ping_enabled is True
        ):
            APP.state.ping_enabled = ping_enabled is True
            changed = True

        processes_enabled: Any = body.get("processes_enabled")
        if processes_enabled is not None and APP.state.history_processes_enabled != (
            processes_enabled is True
        ):
            APP.state.history_processes_enabled = processes_enabled is True
            changed = True

        # Only interval changes reset the timer; other options apply to the next sample.
        if interval_changed and APP.state.history_task is not None:
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

    async def ping_all() -> list[float | None]:
        try:
            return await asyncio.wait_for(
                asyncio.gather(
                    *(asyncio.to_thread(_run_ping, t) for t in PING_TARGETS)
                ),
                timeout=8,
            )
        # asyncio.TimeoutError is a distinct class from builtin TimeoutError before
        # Python 3.11 (the declared floor is 3.10), so catch both.
        except (asyncio.TimeoutError, TimeoutError):
            return [None] * len(PING_TARGETS)

    # Under the UI's fastest poll (1 s): one round of pings serves every browser.
    results: list[float | None] = await _cached("ping", 0.9, ping_all)
    targets: dict[str, Any] = {}
    target: str
    latency: float | None
    for target, latency in zip(PING_TARGETS, results):
        if latency is not None:
            targets[target] = {"latency_ms": round(latency, 2)}
        else:
            targets[target] = {"error": "timeout"}
    return Response(
        content=json.dumps({"targets": targets}),
        media_type="application/json",
    )


async def services_snapshot(now: float) -> dict[str, Any]:
    """Capture state on the loop, then count successful checks in a worker.

    Round dictionaries are never mutated after publication by _note_service_round;
    copying the deque is enough to isolate append/prune operations. Latest results
    are small and copied too, so the worker never reads changing service state.
    """
    return await asyncio.to_thread(
        _services_snapshot,
        now,
        tuple(HTTP_TARGETS),
        tuple(_services_rounds),
        {name: dict(latest) for name, latest in _services_latest.items()},
        _services_started,
        SERVICES_CHECK_INTERVAL,
        SERVICES_TIMEOUT,
        tuple(SERVICES_UPTIME_HOURS),
    )


def _services_snapshot(
    now: float,
    configured: tuple[HttpTarget, ...],
    rounds: tuple[ServiceRound, ...],
    latest_by_name: dict[str, dict[str, Any]],
    started: float | None,
    interval: int,
    timeout: float,
    windows: Sequence[int],
) -> dict[str, Any]:
    """The Services card's data: each configured target's newest result and its
    uptime counts over SERVICES_UPTIME_HOURS. A target not checked yet has
    ``ok: null``."""
    names: list[str] = [t.name for t in configured]
    uptime: dict[str, dict[str, dict[str, int]]] = services_uptime(
        rounds, names, now, windows
    )
    down_since: dict[str, float | None] = services_down_since(rounds, names)
    targets: list[dict[str, Any]] = []
    target: HttpTarget
    for target in configured:
        latest: dict[str, Any] = latest_by_name.get(target.name, {})
        targets.append(
            {
                "name": target.name,
                "url": target.url,
                "ok": latest.get("ok"),
                "status": latest.get("status"),
                "ms": latest.get("ms"),
                "error": latest.get("error"),
                "checked": latest.get("checked"),
                # Epoch of the first failed check of the current outage, or null.
                "down_since": down_since[target.name],
                "uptime": uptime[target.name],
            }
        )
    # The checker died or hung: the newest results are old news, and a reader
    # must not take the last "up" for the present. The clock runs from the
    # newest round or the checker's start, whichever is later: a first round
    # that never ends is not "pending" forever, and a restart after downtime is
    # not stale on account of the rounds read back from before it. Rounds are
    # stamped at their start, and one with a hanging target lasts two timeouts
    # (the retry): hence four timeouts of allowance, not two.
    marks: list[float] = [
        t
        for t in (
            rounds[-1][0] if rounds else None,
            started,
        )
        if t is not None
    ]
    stale: bool = bool(marks) and (now - max(marks) > 3 * interval + 4 * timeout)
    return {
        "interval": interval,
        "now": now,
        "stale": stale,
        "targets": targets,
    }


@APP.get("/api/services")
async def services() -> Response:
    """Each http_targets service: up or down at the last check, and uptime over
    the last hour, day and week."""
    return Response(
        content=json.dumps(await services_snapshot(time.time())),
        media_type="application/json",
    )


@APP.get("/api/services/history")
async def services_history(hours: float = 24) -> Response:
    """Passed and failed checks per target in equal time buckets over the last
    ``hours``, for the outage grid. ``truncated`` means the log could not be
    read back that far, so the oldest buckets are empty."""
    hours = _clamp_hours(hours)
    now: float = time.time()
    start: float
    bucket: int
    count: int
    start, bucket, count = services_bucket_plan(hours, now, SERVICES_CHECK_INTERVAL)
    # Published round dictionaries are immutable (see services_snapshot). Capture
    # the deque and target names before yielding, including the fallback on a
    # failed disk read, so the worker never traverses changing live state.
    rounds: Iterable[ServiceRound] = tuple(_services_rounds)
    names: tuple[str, ...] = tuple(t.name for t in HTTP_TARGETS)
    truncated: bool = False
    if names and hours > SERVICES_UPTIME_HOURS[-1]:
        e: Exception
        try:
            rounds, truncated = await asyncio.to_thread(_read_service_rounds, start)
        except Exception as e:
            logger.error("Service history: cannot read %s: %s", SERVICES_FILE, e)
            truncated = True

    def encode() -> str:
        return json.dumps(
            {
                "start": start,
                "bucket_sec": bucket,
                "count": count,
                "now": now,
                "truncated": truncated,
                "targets": bucket_services(rounds, names, start, bucket, count),
            }
        )

    return Response(
        content=await asyncio.to_thread(encode), media_type="application/json"
    )


_cuda_version: str = ""  # nvidia-smi's CUDA version once read (see _get_gpu_info)


def _get_gpu_info() -> list[dict[str, int | float | str | None]]:
    """Detailed GPU info from nvidia-smi (sync, called from thread pool).

    One shape: a list of GPU dicts, empty when nvidia-smi is missing, times out
    or fails (like get_gpu_stats); the reason goes to the log. Unsupported
    metric fields stay None so, for example, unavailable MIG utilization does
    not hide the GPU's usable memory totals from the live charts.
    """
    e: subprocess.TimeoutExpired | OSError
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,driver_version,fan.speed",
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
        _log_nvidia_failure(result)
        return []

    # Get CUDA version from nvidia-smi header. It changes only with the driver,
    # so once read it is kept: no second nvidia-smi run per request.
    global _cuda_version
    cuda_version: str = _cuda_version
    line: str
    if not cuda_version:
        try:
            cuda_result: subprocess.CompletedProcess[str] = subprocess.run(
                ["nvidia-smi"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in cuda_result.stdout.split("\n"):
                if "CUDA Version:" in line:
                    # Extract version like "12.4" from "CUDA Version: 12.4"
                    version_parts: list[str] = (
                        line.split("CUDA Version:")[1].strip().split()
                    )
                    cuda_version = version_parts[0] if version_parts else ""
                    break
            _cuda_version = cuda_version
        except Exception:
            pass  # CUDA version is optional — GPU data still valid

    def metric(value: str) -> float | None:
        try:
            return _num(float(value))
        except ValueError:
            return None

    gpus: list[dict[str, int | float | str | None]] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) >= 9:
            try:
                gpus.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "utilization": metric(parts[2]),  # %
                        "memory_used": metric(parts[3]),  # MiB
                        "memory_total": metric(parts[4]),  # MiB
                        "temperature": metric(parts[5]),  # °C
                        "power_draw": metric(parts[6]),  # W
                        "power_limit": metric(parts[7]),  # W
                        "driver_version": parts[8],
                        "cuda_version": cuda_version,
                        # % of the fan's maximum; None on a passive card ([N/A]).
                        "fan_percent": metric(parts[9]) if len(parts) > 9 else None,
                    }
                )
            except (ValueError, TypeError, IndexError):
                logger.warning("Skipping malformed nvidia-smi line: %s", line)
                continue

    return gpus


@APP.get("/api/gpu-info")
async def gpu_info() -> Response:
    """Detailed GPU info from nvidia-smi; [] when it is unavailable."""
    result: list[dict[str, float | str | None]] = await _cached(
        "gpu-info", 5.0, lambda: asyncio.to_thread(_get_gpu_info)
    )
    return Response(content=json.dumps(result), media_type="application/json")


def _get_cpu_info() -> dict[str, Any]:
    """Get detailed CPU info from /proc/cpuinfo (sync, called from thread pool)."""
    cpuinfo_path: Path = Path("/proc/cpuinfo")
    if not cpuinfo_path.exists():
        return {}

    content: str = cpuinfo_path.read_text()

    model_name: str = ""
    physical_cores: set[tuple[str, str]] = set()
    logical_cores: int = 0
    cache_size: str = ""

    block: str
    for block in re.split(r"\n\s*\n", content.strip()):
        fields: dict[str, str] = {
            key.strip(): value.strip()
            for key, value in (
                line.split(":", 1) for line in block.splitlines() if ":" in line
            )
        }
        model_name = model_name or fields.get("model name", "")
        cache_size = cache_size or fields.get("cache size", "")
        if "processor" in fields:
            logical_cores += 1
            # Core IDs repeat across sockets; block parsing makes field order irrelevant.
            if fields.get("core id"):
                physical_cores.add((fields.get("physical id", ""), fields["core id"]))

    # Try to get L3 cache from sysfs
    l3_cache: str = ""
    l3_path: Path = Path("/sys/devices/system/cpu/cpu0/cache/index3/size")
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
    e: Exception
    try:
        result: dict[str, Any] = await _cached(
            "cpu-info", math.inf, lambda: asyncio.to_thread(_get_cpu_info)
        )
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
    e: Exception
    try:
        ip_out: subprocess.CompletedProcess[str] = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ip_out.returncode == 0:
            interfaces: list[dict[str, Any]] = json.loads(ip_out.stdout)
            iface: dict[str, Any]
            for iface in interfaces:
                name: str = iface.get("ifname", "")
                if name == "lo":
                    continue
                ipv4_addrs: list[str] = []
                ipv6_addrs: list[str] = []
                addr_info: dict[str, Any]
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
        client: httpx.Client
        with httpx.Client(timeout=3) as client:
            try:
                resp: httpx.Response = client.get("https://api.ipify.org")
                if resp.status_code == 200 and resp.text.strip():
                    result["public"]["ipv4"] = resp.text.strip()
            except Exception as e:
                logger.debug("Failed to get public IPv4: %s", e)
            try:
                resp = client.get("https://api64.ipify.org")
                if resp.status_code == 200 and resp.text.strip():
                    addr: str = resp.text.strip()
                    # api64 can return IPv4, including when the first lookup failed.
                    result["public"].setdefault("ipv6" if ":" in addr else "ipv4", addr)
            except Exception as e:
                logger.debug("Failed to get public IPv6: %s", e)
    except Exception as e:
        logger.warning("Failed to create HTTP client for public IP: %s", e)

    if not result["public"].get("ipv4") and not result["public"].get("ipv6"):
        result["public"]["error"] = "lookup failed"

    # --- Tailscale IP ---
    try:
        ts_out: subprocess.CompletedProcess[str] = subprocess.run(
            ["tailscale", "ip"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ts_out.returncode == 0:
            lines: list[str] = [
                ln.strip() for ln in ts_out.stdout.strip().splitlines() if ln.strip()
            ]
            line: str
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


async def _ip_info_cached() -> dict[str, Any]:
    """_get_ip_info, at most once a minute: it starts two processes and asks an
    outside service for the public address. Shared by /api/ip-info and the
    history sampler, so a record's ``ip`` can be up to a minute old."""
    return await _cached("ip-info", 60.0, lambda: asyncio.to_thread(_get_ip_info))


@APP.get("/api/ip-info")
async def ip_info() -> Response:
    """Get IP addresses: LAN, public, and Tailscale."""
    e: Exception
    try:
        result: dict[str, Any] = await _ip_info_cached()
        return Response(content=json.dumps(result), media_type="application/json")
    except Exception as e:
        logger.warning("Failed to get IP info: %s", e)
        return Response(
            content=json.dumps({"error": "Failed to get IP info"}),
            status_code=500,
            media_type="application/json",
        )


def _parse_pmon(
    stdout: str,
) -> dict[tuple[int, int], tuple[float | None, float | None]]:
    """{(gpu index, pid): (sm %, mem %)} from ``nvidia-smi pmon -c 1 -s u``.

    Columns: gpu pid type sm mem enc dec jpg ofa command; '#' lines are headers.
    '-' is "no sample" (the driver had nothing for that pid in the window: an
    idle process, or a GPU that does not support per-process accounting) and
    becomes None, which the callers keep apart from 0.
    """
    out: dict[tuple[int, int], tuple[float | None, float | None]] = {}
    line: str
    for line in stdout.splitlines():
        parts: list[str] = line.split()
        if not parts or parts[0].startswith("#") or len(parts) < 5:
            continue
        try:
            key: tuple[int, int] = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue

        def pct(tok: str) -> float | None:
            try:
                return float(tok)  # '-' -> ValueError -> None
            except ValueError:
                return None

        out[key] = (pct(parts[3]), pct(parts[4]))
    return out


def _gpu_process_utilization() -> dict[
    tuple[int, int], tuple[float | None, float | None]
]:
    """Per-process SM / memory-bandwidth utilization, {} when pmon is unusable.

    ``--query-compute-apps`` has no utilization field; ``pmon`` is the only
    source without accounting mode (root to enable). One 1 s sample, ~0.1 s.
    pmon watches at most four GPUs (its own limit); a fifth gets no rows, so
    those processes read as None, never as 0. Any failure is a missing column,
    not a missing process list.
    """
    e: OSError | subprocess.SubprocessError
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
            capture_output=True,
            text=True,
            # The command column is the kernel's 15-byte comm, which can end
            # mid-character (or be any bytes a process set): never raise on it.
            errors="replace",
            # It takes ~0.1 s; a stalled pmon must not outlast the browser's 5 s
            # fetch timeout, or the whole process list is lost with it.
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("nvidia-smi pmon failed: %s", e)
        return {}
    if result.returncode != 0:
        logger.debug(
            "nvidia-smi pmon exit %d: %s",
            result.returncode,
            result.stderr.strip()[:200],
        )
        return {}
    return _parse_pmon(result.stdout)


def _get_gpu_processes() -> list[dict[str, Any]]:
    """Get processes using GPU from nvidia-smi (sync, called from thread pool).

    # TODO: surface per-line parse warnings to the frontend

    Returns a list of process dicts ([] when nvidia-smi is missing); raises
    when it fails.
    """
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        errors="replace",  # process paths are bytes, not necessarily UTF-8
        timeout=5,
    )
    if result.returncode != 0:
        return []

    # Get GPU index mapping from UUID
    uuid_result: subprocess.CompletedProcess[str] = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if uuid_result.returncode != 0:
        logger.warning(
            "Cannot map GPU processes to devices: nvidia-smi UUID query failed"
        )
        return []
    uuid_to_index: dict[str, int] = {}
    line: str
    for line in uuid_result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts: list[str] = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                uuid_to_index[parts[1]] = int(parts[0])
            except ValueError:
                logger.warning("Skipping GPU UUID line with bad index: %s", line)

    util: dict[tuple[int, int], tuple[float | None, float | None]] = (
        _gpu_process_utilization()
    )

    processes: list[dict[str, Any]] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            e: ValueError | IndexError
            try:
                gpu_uuid: str = parts[0]
                if gpu_uuid not in uuid_to_index:
                    logger.warning("Skipping process on unmapped GPU %s", gpu_uuid)
                    continue
                pid: int = int(parts[1])
                nvidia_name: str = parts[2]
                # Get sanitized process info from /proc
                info: dict[str, str] = get_sanitized_process_info(
                    pid, Path(nvidia_name).name, nvidia_name
                )
                gpu_index: int = uuid_to_index[gpu_uuid]
                sm: float | None
                membw: float | None
                sm, membw = util.get((gpu_index, pid), (None, None))
                processes.append(
                    {
                        "gpu_index": gpu_index,
                        "pid": pid,
                        "name": info["name"],
                        "path": info["path"],
                        "gpu_memory_mib": float(parts[3]),
                        # SM and memory-bandwidth utilization of this pid on this
                        # GPU over pmon's 1 s sample; None when the driver had no
                        # sample for it (an idle process, or pmon unavailable).
                        "gpu_util_percent": sm,
                        "gpu_mem_util_percent": membw,
                    }
                )
            except (ValueError, IndexError) as e:
                logger.warning("Skipping malformed GPU process line: %s (%s)", line, e)

    return processes


@APP.get("/api/gpu-processes")
async def gpu_processes() -> Response:
    """Get processes using GPU from nvidia-smi."""

    async def read() -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(_get_gpu_processes)
        except FileNotFoundError:
            logger.debug("nvidia-smi not found for GPU processes")
        except subprocess.TimeoutExpired:
            logger.warning("nvidia-smi timed out for GPU processes")
        return []

    # Three nvidia-smi runs per read; under the UI's fastest poll (1 s), so one
    # read serves every browser. The empty result is kept too: a box without
    # nvidia-smi would otherwise try to start it on every request.
    result: list[dict[str, Any]] = await _cached("gpu-processes", 0.9, read)
    return Response(content=json.dumps(result), media_type="application/json")


def _run_pidstat(duration: int, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run pidstat synchronously (called from thread pool)."""
    return subprocess.run(
        ["pidstat", "-d", str(duration), "1"],
        capture_output=True,
        text=True,
        errors="replace",  # process names/paths are bytes, not necessarily UTF-8
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},  # stable Average: label and decimal points
    )


def _parse_pidstat(stdout: str) -> str:
    """pidstat -d output as the JSON body served: processes with I/O, named from
    /proc (sync, called from thread pool)."""
    # Parse pidstat output
    # Format: Average:  UID  PID  kB_rd/s  kB_wr/s  kB_ccwr/s  iodelay  Command
    # Fields:    [0]    [1]  [2]    [3]      [4]       [5]       [6]      [7]
    processes: dict[int, dict[str, Any]] = {}

    for line in stdout.strip().split("\n"):
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
                info: dict[str, str] = get_sanitized_process_info(pid, command, "")
                processes[pid] = {
                    "pid": pid,
                    "read_kbps": read_kbps,
                    "write_kbps": write_kbps,
                    "name": info["name"],
                    "path": info["path"],
                }
        except (ValueError, IndexError):
            continue

    return json.dumps(list(processes.values()))


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

    e: Exception
    try:
        # Clamp duration to reasonable range
        duration = max(1, min(duration, 10))

        # pidstat -d: disk I/O stats
        # Args: interval (seconds), count (number of samples)
        result: subprocess.CompletedProcess[str] = await asyncio.to_thread(
            _run_pidstat, duration=duration, timeout=duration + 5
        )

        if result.returncode != 0:
            error_msg: str = result.stderr.strip() or f"Exit code {result.returncode}"
            logger.warning("pidstat failed: %s", error_msg)
            return Response(
                content=json.dumps({"error": "pidstat failed", "detail": error_msg}),
                media_type="application/json",
            )

        body: str = await asyncio.to_thread(_parse_pidstat, result.stdout)
        return Response(content=body, media_type="application/json")

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
        errors="replace",  # process names/paths are bytes, not necessarily UTF-8
        timeout=timeout,
    )


def _parse_nethogs(stdout: str) -> str:
    """nethogs -t output as the JSON body served: processes with traffic, named
    from /proc (sync, called from thread pool)."""
    # Parse nethogs trace mode output
    # Format: <path>/<pid>/<uid>\t<sent_kb/s>\t<recv_kb/s>
    processes: dict[int, dict[str, Any]] = {}

    for line in stdout.strip().split("\n"):
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
                pid: int = int(parts[-2])
                # The path is the fallback when the process exits before
                # /proc is read. nethogs -l (and some older builds) print the
                # whole command line here, whose arguments may hold secrets:
                # keep only the text before the first whitespace.
                exe_path: str = (parts[0].split() or [""])[0]
                program: str = exe_path.rsplit("/", 1)[-1]
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
                info: dict[str, str] = get_sanitized_process_info(
                    pid, program, exe_path
                )
                processes[pid] = {
                    "pid": pid,
                    "name": info["name"],
                    "path": info["path"],
                    "sent_kbps": sent_kbps,
                    "recv_kbps": recv_kbps,
                }
        except (ValueError, IndexError):
            continue

    return json.dumps(
        [p for p in processes.values() if p["sent_kbps"] > 0 or p["recv_kbps"] > 0]
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

    e: Exception
    try:
        # nethogs requires CAP_NET_ADMIN or root
        # -t: trace mode (stdout instead of ncurses)
        # -c N: run N cycles then exit
        # -d 1: 1 second delay between refreshes
        cycles: int = max(1, min(duration, 60))
        timeout: int = int(cycles * 1.5) + 5
        result: subprocess.CompletedProcess[str] = await asyncio.to_thread(
            _run_nethogs, cycles=cycles, timeout=timeout
        )

        if result.returncode != 0:
            error_msg: str = result.stderr.strip() or f"Exit code {result.returncode}"
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
                            "detail": "Run ./install.sh --grant-capabilities (README: Process permissions)",
                        }
                    ),
                    media_type="application/json",
                )
            return Response(
                content=json.dumps({"error": "nethogs failed", "detail": error_msg}),
                media_type="application/json",
            )

        body: str = await asyncio.to_thread(_parse_nethogs, result.stdout)
        return Response(content=body, media_type="application/json")

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


def _sanitized_processlist(processes: list[dict[str, Any]]) -> str:
    """Glances' process list as the JSON body served: display_name/display_path
    from /proc, and no cmdline (sync, called from thread pool)."""
    proc: dict[str, Any]
    for proc in processes:
        pid: Any = proc.get("pid")
        if pid:
            name: str = proc.get("name", "unknown")
            info: dict[str, str] = get_sanitized_process_info(pid, name)
            proc["display_name"] = info["name"]
            proc["display_path"] = info["path"]
        # Remove cmdline to prevent leaking sensitive data
        proc.pop("cmdline", None)
    return json.dumps(processes)


@APP.get("/api/processlist")
async def processlist() -> Response:
    """Get process list from Glances with sanitized display info."""
    glances_url: str = APP.state.glances_url
    client: httpx.AsyncClient
    async with _glances_client() as client:
        error: httpx.RequestError
        try:
            resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/processlist", timeout=10.0
            )
            if resp.status_code != 200:
                return Response(content=resp.content, status_code=resp.status_code)

            # One /proc read per process, and a large body: off the event loop.
            body: str = await asyncio.to_thread(
                lambda: _sanitized_processlist(resp.json())
            )
            return Response(content=body, media_type="application/json")
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
    client: httpx.AsyncClient
    async with _glances_client() as client:
        error: httpx.RequestError
        try:
            resp: httpx.Response = await client.get(
                f"{glances_url}/api/4/fs", timeout=10.0
            )
            if resp.status_code == 200:
                body: str = await asyncio.to_thread(
                    lambda: json.dumps(_shape_fs(resp.json(), True))
                )
                return Response(content=body, media_type="application/json")
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


def _redact_cron_subject(subject: str) -> str:
    """Reduce cron's "Cron <user@host> <command line>" subject to the program's
    name: the command line can carry a token or password, and subjects are served
    unauthenticated. Other subjects pass through unchanged.

    Words are split the way the shell would (quotes, and operators like > and &&),
    so a quoted value with spaces stays one word. Anything that can't be split
    that way, $(...) and backticks, or a name that isn't a plain file name leaves
    only "Cron <user@host>": the fallback hides the command rather than guess.
    """
    m: re.Match[str] | None = re.fullmatch(r"(Cron <[^>]*>)\s+(.*)", subject, re.S)
    if m is None:
        return subject
    command: str = m.group(2)
    if "$(" in command or "`" in command:
        return m.group(1)
    lexer: shlex.shlex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words: list[str]
    try:
        words = list(lexer)
    except ValueError:  # unbalanced quotes
        return m.group(1)
    # Leading VAR=value words are environment assignments, not the program (and a
    # usual place for a secret). One with $ may be ${X:-a b}, which the shell keeps
    # as one word but shlex splits, so a piece of its value would become the program.
    while words and re.match(r"[A-Za-z_]\w*=", words[0]):
        if "$" in words.pop(0):
            return m.group(1)
    if not words:
        return m.group(1)
    name: str = _sanitize_process_argv(words)["name"]
    if not re.fullmatch(r"[\w.+-]+", name):
        return m.group(1)
    return f"{m.group(1)} {name}"


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
        box: mailbox.mbox = mailbox.mbox(path, create=False)
        try:
            keys: list[str] = box.keys()
            result["count"] = len(keys)
            key: str
            for key in reversed(keys[-limit:]):
                e: Exception
                try:
                    msg: email.message.EmailMessage[Any, Any] = (
                        email.message_from_bytes(
                            box.get_bytes(key), policy=email.policy.default
                        )
                    )
                    # With policy.default the header is a DateHeader whose .datetime
                    # is None on garbage; the header itself is None when absent.
                    d: Any = msg["Date"]
                    entry: dict[str, Any] = {
                        "date_epoch": (
                            d.datetime.timestamp()
                            if d is not None and d.datetime is not None
                            else None
                        ),
                        "subject": _redact_cron_subject(str(msg.get("Subject", "")))
                        or "(no subject)",
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


_mail_cache: dict[str, Any] = {"key": None, "result": None}


def _get_mail_cached() -> dict[str, Any]:
    """Re-read the spool when its contents, identity or access metadata changes."""
    try:
        st: os.stat_result = os.stat(MAIL_SPOOL)
    except OSError:
        return _get_mail()  # missing or unreadable: cheap, and reports why
    # Access metadata matters even on filesystems whose timestamps are coarse:
    # fixing an unreadable spool must invalidate a cached permission error.
    key: tuple[int, ...] = (
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mode,
        st.st_uid,
        st.st_gid,
    )
    if _mail_cache["key"] != key:
        _mail_cache["result"] = _get_mail()
        _mail_cache["key"] = key
    return _mail_cache["result"]


@APP.get("/api/mail")
async def mail() -> Response:
    """Local system mail: count and newest subjects. Always 200; ``status`` says why not."""
    result: dict[str, Any] = await asyncio.to_thread(_get_mail_cached)
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
    result: dict[str, Any] = {
        "status": "ok",
        "detail": None,
        "age_sec": None,
        "path": path,
    }
    e: OSError | ValueError
    try:
        f: BinaryIO
        with open(path, "rb") as f:
            state: Any = json.load(f)
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
    ts: Any = state.get("ts")
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


COLLECTOR_UNIT_DIRS: Final[tuple[Path, ...]] = tuple(
    Path(path)
    for path in (
        "/etc/systemd/system",
        "/run/systemd/system",
        "/usr/local/lib/systemd/system",
        "/usr/lib/systemd/system",
        "/lib/systemd/system",
    )
)
_collectors_seen: set[str] = set()


def _collector_expected(source: str, snapshot: dict[str, Any]) -> bool:
    """An optional collector is absent only when there is no evidence of it.

    Once observed (including persisted tracker state), losing its snapshot is an
    outage. Installed or masked units also remain expected; failed discovery is
    unknown, never proof that monitoring was deliberately left out.
    """
    if snapshot.get("status") != "missing":
        _collectors_seen.add(source)
    if source in _collectors_seen or source in _alarm_memory:
        return True
    unit: str = "horatio-collect" if source == "state" else "horatio-sensors"
    for directory in COLLECTOR_UNIT_DIRS:
        for suffix in (".timer", ".service"):
            try:
                (directory / (unit + suffix)).lstat()
                return True
            except FileNotFoundError:
                continue
            except OSError:
                return True
    return False


@APP.get("/api/storage-health")
async def storage_health() -> Response:
    """The collector's hardware health snapshot with alarms. Always 200; ``status`` says
    freshness. ``sensor_alarms`` / ``sensor_notes`` are the server's own (hwmon fan
    rules, setup hints), present whatever the collector's state, so the Hardware card
    shows them even on a box without the collector."""
    result: dict[str, Any] = with_alarm_history(
        "state", await asyncio.to_thread(_get_storage_health), _alarm_memory
    )
    result["expected"] = await asyncio.to_thread(_collector_expected, "state", result)
    hw: dict[str, Any] = await asyncio.to_thread(server_findings, False)
    result["sensor_alarms"] = hw["alarms"]
    # The same alarms with since when (the server's alarm history).
    result["sensor_alarm_items"] = [
        {k: v for k, v in row.items() if k != "source" and v is not None}
        for row in alarm_rows("dashboard", hw["alarm_items"], _alarm_memory)
    ]
    # The server's own alarms it could not look at again (a ruled fan whose chip
    # stopped answering, a pool zpool could not read): unchecked, not clear.
    result["sensor_alarm_held"] = [
        {k: v for k, v in row.items() if k != "source" and v is not None}
        for row in held_rows("dashboard", None, _alarm_memory)
    ]
    result["sensor_notes"] = hw["notes"]
    result["sensor_status"] = hw.get("status", "ok")
    # ZFS pools as the server read them: {status ok|error|absent, error?, pools}.
    result["zfs"] = hw["zfs"]
    return Response(content=json.dumps(result), media_type="application/json")


@APP.get("/api/sensors")
async def sensors_snapshot() -> Response:
    """The 60 s fans / temps / power snapshot. Always 200; ``status`` says freshness."""
    result: dict[str, Any] = with_alarm_history(
        "sensors",
        await asyncio.to_thread(_get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC),
        _alarm_memory,
    )
    result["expected"] = await asyncio.to_thread(_collector_expected, "sensors", result)
    result["power_rows"] = collector_power_rows(result)
    return Response(content=json.dumps(result), media_type="application/json")


# ---------------------------------------------------------------------------
# Hardware sensors from hwmon (hwmon.py): temperatures, fans, power. World-readable,
# so the server reads them itself; the root collectors keep only what needs root.
# ---------------------------------------------------------------------------

HWMON_CACHE_SEC: Final[float] = 5.0  # several cards and the recorder read per poll
_hwmon_lock: _thread.LockType = threading.Lock()
_hwmon_cache: dict[str, Any] = {"at": -math.inf, "data": None}
# Fans seen above 0 RPM since the server started: one of these at 0 has stopped,
# it is not an empty header, so hide_if_zero no longer hides it.
_fans_spun: set[str] = set()


def _sensor_hide_patterns() -> list[re.Pattern[str]]:
    """config.json sensor_hide: regexps matched (from the start, like Glances'
    ``hide``) against a sensor's own name and its sensor_labels name. Empty by
    default. A pattern that does not compile is logged and skipped."""
    raw: Any = _CONFIG.get("sensor_hide")
    patterns: list[re.Pattern[str]] = []
    text: Any
    for text in raw if isinstance(raw, list) else []:
        if not isinstance(text, str):
            continue
        try:
            patterns.append(re.compile(text))
        except re.error as e:
            logger.error(
                "config sensor_hide: %r is not a regexp (%s); ignored", text, e
            )
    return patterns


_SENSOR_HIDE: list[re.Pattern[str]] = []  # set at startup, below TEMP_LABELS


def _sensor_hidden(label: str) -> bool:
    names: list[str] = [label, TEMP_LABELS.get(label, label)]
    if label.startswith("@") and label.count("/") >= 2:
        _, chip, channel = label.rsplit("/", 2)
        friendly: str = f"{chip} {channel}"
        names.extend((friendly, TEMP_LABELS.get(friendly, friendly)))
    return any(p.match(n) for p in _SENSOR_HIDE for n in names)


def get_hw_sensors() -> dict[str, Any]:
    """``rows`` (Glances-shaped, fan rules applied), ``alarms`` and ``notes`` (sync).

    The fan rules and the setup hints run here, in the one place that reads the
    sensors, so the dashboard, history and /api/alarms always agree."""
    with _hwmon_lock:
        now: float = time.monotonic()
        if now - _hwmon_cache["at"] < HWMON_CACHE_SEC and _hwmon_cache["data"]:
            return _hwmon_cache["data"]
        raw: list[dict[str, Any]] = []
        hidden: set[str] = set()
        for r in hwmon.read_sensors():
            identity: str = r.get("id") or r["label"]
            if _sensor_hidden(identity) or _sensor_hidden(r["label"]):
                hidden.update((identity, r["label"]))
            else:
                raw.append(r)

        def excluded(name: str) -> bool:
            return (
                name in hidden
                or _sensor_hidden(name)
                or any(TEMP_LABELS.get(n) == name for n in hidden)
            )

        _fans_spun.update(
            r.get("id") or r["label"]
            for r in raw
            if r.get("type") == hwmon.FAN and r["value"] > 0
        )
        fan_rules: Any = _CONFIG.get("fans")
        fan_rules = {
            n: rule
            for n, rule in (fan_rules.items() if isinstance(fan_rules, dict) else [])
            if n == "default" or not excluded(n)
        }
        known: set[str] = {
            key.removeprefix("fan/")
            for key in _alarm_memory.get("dashboard", {}).get("alarms", {})
            if key.startswith("fan/")
        }
        rows, items, notes, checked = hwmon.fan_status(
            raw,
            fan_rules,
            _fans_spun,
            TEMP_LABELS,
            known={n for n in known if not excluded(n)},
        )
        # Deliberate configuration exclusions can resolve remembered alarms;
        # temporarily unreadable sensors cannot.
        checked.extend(f"fan/{n}" for n in known if excluded(n))
        notes = [*notes, *hwmon.setup_notes(raw)]
        data: dict[str, Any] = {
            "rows": rows,
            "alarms": [item["text"] for item in items],
            # The same alarms with their keys, for the alarm history, and which
            # fans were really read: a fan that has stopped answering is not a fan
            # that is fine again.
            "alarm_items": items,
            "checked": checked,
            "notes": notes,
        }
        _hwmon_cache.update(at=now, data=data)
        return data


@APP.get("/api/4/sensors")
async def hw_sensors() -> Response:
    """Temperatures, fans and power from hwmon, in Glances' sensors shape (it used to
    be Glances' plugin; the Lua backend serves the same shape). Power rows carry
    ``total``: true only for a reading of the whole machine."""
    data: dict[str, Any] = await asyncio.to_thread(get_hw_sensors)
    return Response(content=json.dumps(data["rows"]), media_type="application/json")


HOST_CHECK_CACHE_SEC: Final[float] = 30.0  # the tracker's pass; modinfo on a mismatch
_host_lock: _thread.LockType = threading.Lock()
_host_cache: dict[str, Any] = {"at": -math.inf, "data": None}


def _host_findings() -> hostcheck.Findings:
    """hostcheck.host_findings, cached (sync)."""
    with _host_lock:
        now: float = time.monotonic()
        if now - _host_cache["at"] < HOST_CHECK_CACHE_SEC and _host_cache["data"]:
            return _host_cache["data"]
        data: hostcheck.Findings = hostcheck.host_findings()
        _host_cache.update(at=now, data=data)
        return data


ZFS_CACHE_SEC: Final[float] = 60.0
# Pools this server has seen, so one that disappears is an alarm (zfs.py).
ZFS_SEEN_FILE: Final[Path] = LOGS_DIR / "zfs-pools.json"
_zfs_lock: _thread.LockType = threading.Lock()
_zfs_cache: dict[str, Any] = {"at": -math.inf, "data": None}


def get_zfs() -> tuple[hostcheck.Findings, dict[str, Any]]:
    """zfs.zfs_findings with config.json zfs_pools / zfs_scrub_max_days, cached
    (sync): zpool runs at most once a minute."""
    with _zfs_lock:
        now: float = time.monotonic()
        if now - _zfs_cache["at"] < ZFS_CACHE_SEC and _zfs_cache["data"]:
            return _zfs_cache["data"]
        data: tuple[hostcheck.Findings, dict[str, Any]] = zfs.zfs_findings(
            _CONFIG.get("zfs_pools"),
            _CONFIG.get("zfs_scrub_max_days"),
            ZFS_SEEN_FILE,
        )
        _zfs_cache.update(at=now, data=data)
        return data


def _cached_host_checks() -> tuple[hostcheck.Findings, dict[str, Any], str]:
    """Read the tracker's latest checks without making HTTP requests wait on tools."""
    now: float = time.monotonic()
    host_cache: dict[str, Any] = _host_cache.copy()
    zfs_cache: dict[str, Any] = _zfs_cache.copy()
    host: hostcheck.Findings = host_cache["data"] or hostcheck.Findings(
        notes=["host checks pending"]
    )
    pools, summary = zfs_cache["data"] or (
        hostcheck.Findings(notes=["ZFS check pending"]),
        {"status": "pending", "pools": []},
    )
    found: hostcheck.Findings = host.merge(pools)
    status: str = "ok" if host_cache["data"] and zfs_cache["data"] else "pending"
    if host_cache["data"] and now - host_cache["at"] > 3 * HOST_CHECK_CACHE_SEC:
        found.notes.append("host checks stale: waiting for the alarm tracker")
        status = "stale"
    if zfs_cache["data"] and now - zfs_cache["at"] > 3 * ZFS_CACHE_SEC:
        found.notes.append("ZFS check stale: waiting for the alarm tracker")
        summary = {**summary, "status": "stale"}
        status = "stale"
    return found, summary, status


def server_findings(refresh: bool = True) -> dict[str, Any]:
    """Everything the server decides itself, as one alarm source ("dashboard"):
    the hwmon fan rules and setup hints (get_hw_sensors) plus the host checks
    (hostcheck: a pending reboot, an NVIDIA driver/library mismatch; zfs: pool
    health and scrubs). The same
    shape as get_hw_sensors without ``rows`` (sync)."""
    hw: dict[str, Any] = get_hw_sensors()
    host: hostcheck.Findings
    if refresh:
        host = _host_findings()
        pools, summary = get_zfs()
        host = host.merge(pools)
        status = "ok"
    else:
        host, summary, status = _cached_host_checks()
    items: list[dict[str, Any]] = [*hw["alarm_items"], *host.alarms]
    return {
        "status": status,
        "zfs": summary,
        "alarms": [item["text"] for item in items],
        "alarm_items": items,
        "checked": [*hw["checked"], *host.checked],
        "notes": [*hw.get("notes", []), *host.notes],
    }


def collector_power_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The collector snapshot's Watts rows as {label, value, total}.

    A collector from this release writes ``unit`` and ``total``; an older one only
    ``reading`` ("238 Watts") and ``entity`` ("rapl" for the CPU/DRAM parts), so for
    one release both are read. The same capture also carries PSU amps."""
    rows: list[dict[str, Any]] = []
    psu: list[dict[str, Any]] = []
    r: Any
    for r in snapshot.get("power") or []:
        if not isinstance(r, dict):
            continue
        # A missing reading must retain its role, including older snapshots that
        # only marked PSU inputs when their units could be read.
        if r.get("psu_input") or re.search(
            r"^(?:ps|psu|pws)[ _]?\d+.*\binput[ _]+power\b",
            str(r.get("label", r.get("name", ""))),
            re.IGNORECASE,
        ):
            psu.append(r)
            continue
        if not _number(r.get("value")):
            continue
        watts: bool = (
            r["unit"] == "W"
            if "unit" in r
            else str(r.get("reading", "")).endswith("Watts")
        )
        if not watts:
            continue
        total: bool = bool(r["total"]) if "total" in r else r.get("entity") != "rapl"
        rows.append({"label": r.get("label", ""), "value": r["value"], "total": total})
    # Per-PSU wall inputs: their sum is the whole box, used only without a total.
    if (
        psu
        and all(_number(r.get("value")) and r.get("unit") == "W" for r in psu)
        and not any(
            isinstance(item, dict)
            and str(item.get("key", "")).split("#", 1)[0]
            in ("capture/power", "unparsed/power")
            for item in snapshot.get("alarm_items") or []
        )
        and not any(row["total"] for row in rows)
    ):
        rows.append(
            {"label": "PSU input", "value": sum(r["value"] for r in psu), "total": True}
        )
    return rows


# ---------------------------------------------------------------------------
# Alarm history: when each alarm started, changed and cleared.
#
# Three places decide that something is wrong: the two root collectors (each
# snapshot's ``alarms``, with a stable key per alarm in ``alarm_items``) and this
# server (hwmon fan rules). None of them remembers anything: a snapshot says
# what is wrong now. The remembering is done here, once, for all three, so every
# alarm has a start time whoever raised it and there is one log of what
# happened: logs/events.jsonl, one line per alarm raised, changed in value or
# cleared, plus boots and gaps. Nothing is written while an alarm just stays.
#
# Services are not in the log: logs/services.jsonl already has every check.
# ---------------------------------------------------------------------------

EVENTS_FILE: Final[Path] = LOGS_DIR / "events.jsonl"
# What the tracker knows between runs and across restarts: per source the last
# snapshot it saw and every alarm it holds, with ``since``. Rewritten in place.
ALARM_MEMORY_FILE: Final[Path] = LOGS_DIR / "alarms-state.json"
ALARM_TRACK_INTERVAL: Final[float] = 30.0
# Per source: seconds between its snapshots (three missed is a gap; None for the
# server's own rules, judged on every pass), and how many snapshots in a row an
# alarm must be absent before it counts as cleared. One for the 15-min run; three
# for the 60 s one and for the fan rules, so a value on its threshold does not
# write two lines a minute.
ALARM_SOURCES: Final[dict[str, tuple[float | None, int]]] = {
    "state": (900.0, 1),
    "sensors": (60.0, 3),
    "dashboard": (None, 3),
}
# An alarm that could not be re-checked (a failing drive that is asleep or gone)
# is held this long, then dropped with an ``expired`` line. It also takes this
# many unchecked snapshots in a row, so one snapshot stamped by a clock that was
# far ahead (an RTC before NTP) cannot expire a held alarm.
ALARM_HELD_EXPIRE_SEC: Final[int] = 30 * 86400
ALARM_HELD_EXPIRE_SNAPSHOTS: Final[int] = 3
# No pass for this long means the tracker itself was not running.
ALARM_TRACKER_GAP_SEC: Final[float] = 10 * ALARM_TRACK_INTERVAL
EVENTS_RECENT: Final[int] = 50
# Read from the end of the log: 50 events are a few KiB, so this is generous.
EVENTS_TAIL_BYTES: Final[int] = 256 * 1024
_ALARM_FIELDS: Final[tuple[str, ...]] = ("key", "text", "value", "since", "initial")

# Source -> {"ts": its newest snapshot seen, "boot_id", "alarms": {key: entry}}.
# An entry is {key, text, value?, since, initial?, boot?}; one that is absent but
# not known to be gone also has ``absent`` (a count of snapshots) and/or
# ``unchecked_since``. Only the event loop replaces this.
_alarm_memory: dict[str, Any] = {}
_alarm_memory_saved: float = -math.inf
_alarm_update_lock: asyncio.Lock = asyncio.Lock()


def _read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


# The boot this server runs on, for the alarms it decides itself.
_BOOT_ID: Final[str | None] = _read_boot_id()


def _number(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except OverflowError:
        return False


def _epoch(value: Any) -> bool:
    """A number that can be a time: after 1970 and before 2100, so it can always
    be printed as a date and compared."""
    return _number(value) and 0 < value < 4102444800


_SUFFIX: Final[re.Pattern[str]] = re.compile(r"#\d+$")


def _covered(key: str, checked: Sequence[str] | None) -> bool:
    """True when a snapshot says it really evaluated this key (collector/README.md,
    ``checked``). None means the source evaluates everything every time. A ``#2``
    suffix (the collector's mark on a repeated key, e.g. two drives reporting one
    serial) is covered by whatever covers the key without it."""
    key = _SUFFIX.sub("", key)
    return checked is None or any(
        key == prefix or key.startswith(prefix + "/") for prefix in checked
    )


def track_alarms(
    known: dict[str, dict[str, Any]] | None,
    items: Sequence[Any],
    checked: Sequence[str] | None,
    now: float,
    clear_after: int,
    boot_id: str | None = None,
    hidden: Collection[str] = (),
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """One source's alarms as of ``now`` against what was known of them: the new
    knowledge and the events between the two. Pure; ``known`` is not changed.

    ``known`` is None the first time a source is seen: its alarms were already
    there, so they are ``initial`` (the real start is unknown). An alarm that is
    gone is ``cleared`` only if the snapshot ``checked`` it, and only after
    ``clear_after`` snapshots in a row without it; until then, and for as long as
    it is not checked, it stays known. ``hidden`` are keys the source says still
    exist though it does not list them (journal messages past the ones it
    prints): they remain active. Each entry remembers the boot it was last
    seen on, so a clear that follows a reboot says so: counters that reset at
    boot (ECC, failed units) take their alarms with them."""
    before: dict[str, dict[str, Any]] = dict(known or {})
    after: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    def event(kind: str, entry: dict[str, Any], **more: Any) -> None:
        events.append(
            {
                "event": kind,
                **{k: entry[k] for k in _ALARM_FIELDS if k in entry},
                **more,
            }
        )

    item: Any
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or item["key"] in after  # the same key twice: the first one counts
        ):
            continue
        entry: dict[str, Any] = {"key": item["key"], "text": str(item.get("text", ""))}
        # A value is a count or a short word; anything else (NaN, a list) is dropped.
        if _number(item.get("value")) or isinstance(item.get("value"), str):
            entry["value"] = item["value"]
        old: dict[str, Any] | None = before.pop(entry["key"], None)
        requires: Any = item.get("requires")
        if (
            isinstance(requires, list)
            and requires
            and all(isinstance(k, str) and k for k in requires)
        ):
            # An aggregate that remains faulty cannot forget a missing member.
            entry["requires"] = sorted(
                set(requires) | set((old or {}).get("requires", []))
            )
        elif old and "requires" in old:
            entry["requires"] = old["requires"]
        if old is None:
            entry["since"] = now
            if known is None:
                entry["initial"] = True
            event("raised", entry)
        else:
            entry["since"] = old["since"]
            if old.get("initial"):
                entry["initial"] = True
            if "value" in entry and "value" in old and entry["value"] != old["value"]:
                event("changed", entry, was=old["value"])
        if boot_id:
            entry["boot"] = boot_id
        after[entry["key"]] = entry

    key: str
    for key, old in before.items():
        gone: dict[str, Any] = {
            k: old[k] for k in (*_ALARM_FIELDS, "boot", "requires") if k in old
        }
        if key in hidden:
            # Hidden means positively present, so earlier failed checks and
            # absences no longer apply to this occurrence.
            after[key] = gone
            continue
        absent: int = int(old.get("absent", 0))
        if _covered(key, checked) or (
            old.get("requires") and all(_covered(k, checked) for k in old["requires"])
        ):
            absent += 1
            if absent < clear_after:
                after[key] = {**gone, "absent": absent}
                continue
            # A clock that stepped back must not give a negative duration.
            more: dict[str, Any] = {"active_for_s": max(0, int(now - gone["since"]))}
            if boot_id and gone.get("boot") and gone["boot"] != boot_id:
                more["after_boot"] = True
            event("cleared", gone, **more)
        else:
            unchecked_since: float = old.get("unchecked_since", now)
            unchecked: int = int(old.get("unchecked", 0)) + 1
            if (
                now - unchecked_since > ALARM_HELD_EXPIRE_SEC
                and unchecked >= ALARM_HELD_EXPIRE_SNAPSHOTS
            ):
                event("expired", gone, unchecked_since=unchecked_since)
            else:
                # The absences counted so far are kept: a source that checks a key
                # only every other time must still be able to clear it.
                after[key] = {
                    **gone,
                    "unchecked_since": unchecked_since,
                    "unchecked": unchecked,
                    **({"absent": absent} if absent else {}),
                }
    return after, events


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def observe_alarms(
    memory: dict[str, Any],
    source: str,
    items: Sequence[Any],
    checked: Sequence[str] | None,
    ts: float,
    boot_id: str | None = None,
    boot: Any = None,
    hidden: Collection[str] = (),
    log_gap: bool = True,
) -> list[dict[str, Any]]:
    """Take in one new snapshot of one source (``ts`` is when it was taken):
    update ``memory[source]`` and return the log lines for what changed.
    ``log_gap`` is False when the tracker itself was away: that a collector's
    snapshots were not seen then says nothing about the collector."""
    cadence: float | None
    clear_after: int
    cadence, clear_after = ALARM_SOURCES[source]
    seen: dict[str, Any] | None = memory.get(source)
    events: list[dict[str, Any]] = []
    rebooted: bool = False
    if seen is not None:
        gap: float = ts - seen["ts"]
        if log_gap and gap > (3 * cadence if cadence else ALARM_TRACKER_GAP_SEC):
            # Nothing looked for this long: the box was off or a timer stopped; on
            # the server's own source, this tracker was not running.
            events.append({"event": "gap", "from": _iso(seen["ts"]), "to": _iso(ts)})
        rebooted = bool(seen.get("boot_id") and boot_id and seen["boot_id"] != boot_id)
        if rebooted and isinstance(boot, dict):
            events.append({"event": "boot", **boot})
    alarms: dict[str, dict[str, Any]]
    changes: list[dict[str, Any]]
    alarms, changes = track_alarms(
        seen["alarms"] if seen is not None else None,
        items,
        checked,
        ts,
        clear_after,
        boot_id,
        hidden,
    )
    memory[source] = {"ts": ts, "boot_id": boot_id, "alarms": alarms}
    stamp: str = _iso(ts)
    return [{"timestamp": stamp, "source": source, **e} for e in events + changes]


def _load_alarm_memory(path: Path = ALARM_MEMORY_FILE) -> dict[str, Any]:
    """What the tracker knew when it last ran (sync). A missing or unreadable file
    is an empty memory: every current alarm is then ``initial`` again."""
    e: OSError | ValueError
    try:
        memory: Any = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.error("Alarm history: cannot read %s (%s); starting over", path, e)
        return {}

    # Only what the tracker itself could have written is taken back, entry by
    # entry: one bad value must not stop every later pass.
    def sound(key: Any, entry: Any) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("key") == key
            and isinstance(entry.get("text"), str)
            and _epoch(entry.get("since"))
            and ("absent" not in entry or isinstance(entry["absent"], int))
            and ("unchecked_since" not in entry or _epoch(entry["unchecked_since"]))
            and ("unchecked" not in entry or isinstance(entry["unchecked"], int))
            and (
                "requires" not in entry
                or isinstance(entry["requires"], list)
                and bool(entry["requires"])
                and all(isinstance(k, str) and k for k in entry["requires"])
            )
        )

    out: dict[str, Any] = {}
    source: Any
    seen: Any
    for source, seen in (memory if isinstance(memory, dict) else {}).items():
        if (
            source in ALARM_SOURCES
            and isinstance(seen, dict)
            and _epoch(seen.get("ts"))
            and isinstance(seen.get("alarms"), dict)
        ):
            out[source] = {
                "ts": seen["ts"],
                "boot_id": seen["boot_id"]
                if isinstance(seen.get("boot_id"), str)
                else None,
                "alarms": {k: a for k, a in seen["alarms"].items() if sound(k, a)},
            }
    return out


def _save_alarm_memory(memory: dict[str, Any], path: Path = ALARM_MEMORY_FILE) -> None:
    """Replace the memory file (sync): written beside it, then renamed."""
    path.parent.mkdir(exist_ok=True)
    tmp: Path = path.with_name(path.name + ".tmp")
    f: BinaryIO
    with open(tmp, "wb") as f:
        f.write(json.dumps(memory).encode())
        f.flush()
        os.fsync(f.fileno())  # an empty file after a power cut would forget every start
    os.replace(tmp, path)


def _collector_alarms(snapshot: dict[str, Any]) -> tuple[list[Any], list[str]] | None:
    """A collector snapshot's (alarm_items, checked), or None when it cannot be
    tracked: no snapshot, an unreadable one, or one from a collector that gives
    its alarms no keys."""
    items: Any = snapshot.get("alarm_items")
    checked: Any = snapshot.get("checked")
    if (
        snapshot.get("schema") != 1
        or not isinstance(items, list)
        or not _epoch(snapshot.get("ts"))
    ):
        return None
    return items, [p for p in checked if isinstance(p, str)] if isinstance(
        checked, list
    ) else []


def _append_events(events: list[dict[str, Any]], path: Path = EVENTS_FILE) -> None:
    """Append the lines of one pass in a single write (sync), so a pass is in the
    log whole or not at all."""
    if not events:
        return
    lines: bytes = b"".join(json.dumps(e).encode("utf-8") + b"\n" for e in events)
    path.parent.mkdir(exist_ok=True)
    f: BinaryIO
    with _history_write_lock, open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":  # a torn line from an earlier failed write
                lines = b"\n" + lines
        f.seek(0, os.SEEK_END)
        f.write(lines)
        f.flush()


def _server_findings_or_none() -> dict[str, Any] | None:
    """server_findings for the tracker (sync): None, logged, when it fails."""
    try:
        return server_findings()
    except Exception:
        logger.exception("alarm history: cannot read the server's own findings")
        return None


async def _finish_alarm_update(operation: asyncio.Task[_T]) -> _T:
    """Keep the writer lock until disk work finishes, even during cancellation."""
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


async def track_alarms_once() -> None:
    await _finish_alarm_update(asyncio.create_task(_track_alarms_locked()))


async def _track_alarms_locked() -> None:
    # Retirement and observation must not overwrite each other's memory or files.
    async with _alarm_update_lock:
        await _track_alarms_once()


async def _track_alarms_once() -> None:
    """One pass: every source that has something new since the last pass.

    The memory moves on only once the pass's lines are written (in one write),
    so a log that cannot be written repeats them later instead of losing them.
    The one way to get a line twice is the server dying between that write and
    saving the memory. A source that cannot be read or makes no sense is skipped
    for this pass; the others are still followed."""
    global _alarm_memory, _alarm_memory_saved
    state: dict[str, Any]
    sensors: dict[str, Any]
    hw: dict[str, Any] | None
    state, sensors, hw = await asyncio.gather(
        asyncio.to_thread(_get_storage_health),
        asyncio.to_thread(_get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC),
        asyncio.to_thread(_server_findings_or_none),
    )
    memory: dict[str, Any] = json.loads(json.dumps(_alarm_memory))
    now: float = time.time()
    # Was this tracker itself away (a restart, the server down)? Then a collector
    # whose snapshots went unseen was not necessarily away too: only the tracker's
    # own gap is logged, on its own source.
    own: dict[str, Any] | None = memory.get("dashboard")
    away: bool = own is None or now - own["ts"] > ALARM_TRACKER_GAP_SEC
    events: list[dict[str, Any]] = []
    source: str
    snapshot: dict[str, Any]
    for source, snapshot in (("state", state), ("sensors", sensors)):
        tracked: tuple[list[Any], list[str]] | None = _collector_alarms(snapshot)
        if tracked is None or memory.get(source, {}).get("ts") == snapshot["ts"]:
            continue
        hidden: Any = snapshot.get("alarm_hidden")
        try:
            events += observe_alarms(
                memory,
                source,
                tracked[0],
                tracked[1],
                snapshot["ts"],
                snapshot.get("boot_id")
                if isinstance(snapshot.get("boot_id"), str)
                else None,
                snapshot.get("boot"),
                [k for k in hidden if isinstance(k, str)]
                if isinstance(hidden, list)
                else (),
                log_gap=not away,
            )
        except Exception:
            logger.exception("alarm history: cannot follow the %s snapshot", source)
    # The server's own findings: when they cannot be read the pass goes on without
    # them (nothing of theirs is cleared), so they never cost the collectors' events.
    if hw is not None:
        try:
            events += observe_alarms(
                memory, "dashboard", hw["alarm_items"], hw["checked"], now, _BOOT_ID
            )
        except Exception:
            logger.exception("alarm history: cannot follow the server's own findings")
    await asyncio.to_thread(_append_events, events)
    collectors_moved: bool = any(
        memory.get(s, {}).get("ts") != _alarm_memory.get(s, {}).get("ts")
        for s in ("state", "sensors")
    )
    _alarm_memory = memory
    # The file also says when the tracker last ran, which is how a restart knows
    # how long it was away: so it is refreshed about once a minute regardless.
    if events or collectors_moved or now - _alarm_memory_saved >= 60:
        await asyncio.to_thread(_save_alarm_memory, memory)
        _alarm_memory_saved = now


async def alarm_tracker() -> None:
    """Background task: follow the alarms of every source for as long as the
    server runs. A pass that fails is logged and the next one tries again."""
    global _alarm_memory
    async with _alarm_update_lock:
        _alarm_memory = await asyncio.to_thread(_load_alarm_memory)
    while True:
        try:
            await track_alarms_once()
        except Exception:
            logger.exception("alarm history: a pass failed")
        await asyncio.sleep(ALARM_TRACK_INTERVAL)


def _read_events(
    path: Path = EVENTS_FILE,
    limit: int = EVENTS_RECENT,
    max_bytes: int = EVENTS_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """The newest ``limit`` lines of the event log, oldest first (sync). Never
    raises: no file, an unreadable one or a torn line is fewer events, not an
    error."""
    try:
        f: BinaryIO
        with open(path, "rb") as f:
            size: int = f.seek(0, os.SEEK_END)
            start: int = max(0, size - max_bytes)
            f.seek(start)
            lines: list[bytes] = f.read().split(b"\n")
    except OSError:
        return []
    if start > 0:
        lines = lines[1:]  # the read began mid-line
    events: list[dict[str, Any]] = []
    line: bytes
    for line in lines:
        try:
            event: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            events.append(event)
    return events[-limit:]


def alarm_rows(
    source: str, items: Any, memory: dict[str, Any], texts: Any = None
) -> list[dict[str, Any]]:
    """A source's current alarms as ``{source, key, text, value, since, initial}``
    rows: what it says now, with since when from the tracker's memory. ``texts``
    is the plain ``alarms`` list of a collector that gives no keys: the text is
    then all there is. Whatever is not what it should be is left out."""
    if not isinstance(items, list):
        items = [
            {"text": t}
            for t in (texts if isinstance(texts, list) else [])
            if isinstance(t, str)
        ]
    known: dict[str, Any] = memory.get(source, {}).get("alarms", {})
    rows: list[dict[str, Any]] = []
    item: Any
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        key: str | None = item["key"] if isinstance(item.get("key"), str) else None
        entry: dict[str, Any] = known.get(key, {}) if key is not None else {}
        value: Any = item.get("value")
        rows.append(
            {
                "source": source,
                "key": key,
                "text": item["text"],
                "value": value if _number(value) or isinstance(value, str) else None,
                "since": entry.get("since"),
                "initial": entry.get("initial"),
            }
        )
    return rows


def held_rows(
    source: str, snapshot: dict[str, Any] | None, memory: dict[str, Any]
) -> list[dict[str, Any]]:
    """The alarms of a source that are absent from its newest snapshot only
    because it could not check them: still alarms as far as anyone knows. None
    when the snapshot is one the tracker does not follow (missing, or from a
    collector whose alarms it cannot read): what it remembers is then old.
    ``snapshot`` None is the server's own source, which is always followed."""
    if snapshot is not None and _collector_alarms(snapshot) is None:
        return []
    return [
        {"source": source, **{k: entry.get(k) for k in _ALARM_FIELDS}}
        | {"unchecked_since": entry["unchecked_since"]}
        for entry in memory.get(source, {}).get("alarms", {}).values()
        if "unchecked_since" in entry
    ]


def with_alarm_history(
    source: str, snapshot: dict[str, Any], memory: dict[str, Any]
) -> dict[str, Any]:
    """A collector snapshot as the API serves it: each ``alarm_items`` entry gains
    ``since`` and ``initial``, and ``alarm_held`` lists what could not be
    re-checked. A snapshot the tracker does not follow is returned as it is."""
    if _collector_alarms(snapshot) is None:
        return snapshot
    return {
        **snapshot,
        "alarm_items": [
            {k: v for k, v in row.items() if k != "source" and v is not None}
            for row in alarm_rows(source, snapshot["alarm_items"], memory)
        ],
        "alarm_held": [
            {k: v for k, v in row.items() if k != "source" and v is not None}
            for row in held_rows(source, snapshot, memory)
        ],
    }


def alarms_snapshot(
    now: float,
    state: dict[str, Any],
    sensors: dict[str, Any],
    dashboard: list[dict[str, Any]],
    services: dict[str, Any],
    memory: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything wrong right now, from the four places that know.

    ``active`` follows the Hardware card's rule: the 15-min snapshot's alarms
    count whenever it can be read (``sources.state.status`` says if it is
    stale), the 60 s snapshot's only while it is fresh, the server's own always,
    a service only while the checks are running. So a source that is not ``ok``
    means "unknown", never "clear". ``held`` are alarms a collector could not
    re-check (a failing drive that is asleep). ``recent`` is the tail of the
    event log."""
    active: list[dict[str, Any]] = alarm_rows(
        "state", state.get("alarm_items"), memory, state.get("alarms")
    )
    held: list[dict[str, Any]] = held_rows("state", state, memory)
    if sensors.get("status") == "ok":
        active += alarm_rows(
            "sensors", sensors.get("alarm_items"), memory, sensors.get("alarms")
        )
        held += held_rows("sensors", sensors, memory)
    active += alarm_rows("dashboard", dashboard, memory)
    held += held_rows("dashboard", None, memory)
    # This one diagnosis can be observed directly by the collector and enriched
    # by the host check. Prefer the latter only while it actually exists.
    if any(item.get("key") == "gpu/driver-mismatch" for item in dashboard):
        active = [
            row
            for row in active
            if row["key"] != "gpu/driver-mismatch" or row["source"] == "dashboard"
        ]
        held = [row for row in held if row["key"] != "gpu/driver-mismatch"]
    target: dict[str, Any]
    for target in [] if services["stale"] else services["targets"]:
        if target["ok"] is False:
            active.append(
                {
                    "source": "services",
                    "key": f"service/{target['name']}",
                    "text": f"{target['name']}: down ({target['error']})",
                    "value": None,
                    "since": target["down_since"],
                    "initial": None,
                }
            )
    return {
        "now": now,
        "sources": {
            "state": {"status": state.get("status"), "age_sec": state.get("age_sec")},
            "sensors": {
                "status": sensors.get("status"),
                "age_sec": sensors.get("age_sec"),
            },
            "dashboard": {"status": "ok"},
            "services": {
                "status": "stale" if services["stale"] else "ok",
                "targets": len(services["targets"]),
            },
        },
        "active": active,
        "held": held,
        "recent": events,
    }


@APP.get("/api/alarms")
async def alarms() -> Response:
    """Every active alarm in one list (both collector snapshots, the server's own
    fan rules and host checks, and the service checks) with since when, plus the newest lines of
    the event log. Always 200; ``sources`` says which could be read."""
    state: dict[str, Any]
    sensors: dict[str, Any]
    hw: dict[str, Any]
    events: list[dict[str, Any]]
    state, sensors, hw, events = await asyncio.gather(
        asyncio.to_thread(_get_storage_health),
        asyncio.to_thread(_get_storage_health, SENSORS_PATH, SENSORS_STALE_SEC),
        asyncio.to_thread(server_findings, False),
        asyncio.to_thread(_read_events),
    )
    now: float = time.time()
    body: dict[str, Any] = alarms_snapshot(
        now,
        state,
        sensors,
        hw["alarm_items"],
        await services_snapshot(now),
        _alarm_memory,
        events,
    )
    # The server's own notes (setup hints, a reboot pending under a week, a scrub
    # running): not alarms, but what a login script or an agent should see too.
    body["notes"] = hw["notes"]
    body["sources"]["dashboard"]["status"] = hw.get("status", "ok")
    body["retirement"] = {
        "supported": True,
        "password_configured": PASSWORD_FILE.exists(),
    }
    return Response(content=json.dumps(body), media_type="application/json")


async def _retire_held_alarm(
    source: str, key: str, since: float, unchecked_since: float
) -> Response:
    """Forget one held occurrence, never suppress future observations of its key."""
    global _alarm_memory, _alarm_memory_saved
    async with _alarm_update_lock:
        entry: dict[str, Any] = (
            _alarm_memory.get(source, {}).get("alarms", {}).get(key, {})
        )
        if (
            entry.get("since") != since
            or entry.get("unchecked_since") != unchecked_since
        ):
            return Response(
                content=json.dumps(
                    {
                        "error": "Alarm changed or is no longer held; refresh and try again"
                    }
                ),
                status_code=409,
                media_type="application/json",
            )
        # Collectors can publish a recurring fault between tracker passes. The
        # memory lock cannot stop that external write, so also check what the API
        # currently serves. A later observation is never suppressed by retirement.
        current: dict[str, Any]
        try:
            if source == "dashboard":
                current = await asyncio.to_thread(server_findings, False)
            else:
                current = await asyncio.to_thread(
                    _get_storage_health,
                    STATE_PATH if source == "state" else SENSORS_PATH,
                    STATE_STALE_SEC if source == "state" else SENSORS_STALE_SEC,
                )
        except Exception:
            logger.exception("Cannot check current findings before retirement")
            return Response(
                content=json.dumps(
                    {"error": "Could not check current alarms; refresh and try again"}
                ),
                status_code=503,
                media_type="application/json",
            )
        hidden: Any = current.get("alarm_hidden")
        if (isinstance(hidden, list) and key in hidden) or any(
            isinstance(item, dict) and item.get("key") == key
            for item in (current.get("alarm_items") or [])
        ):
            return Response(
                content=json.dumps(
                    {
                        "error": "The source currently reports this alarm as active; refresh and review it"
                    }
                ),
                status_code=409,
                media_type="application/json",
            )
        memory: dict[str, Any] = json.loads(json.dumps(_alarm_memory))
        del memory[source]["alarms"][key]
        now: float = time.time()
        event: dict[str, Any] = {
            "timestamp": _iso(now),
            "source": source,
            "event": "retired",
            **{field: entry[field] for field in _ALARM_FIELDS if field in entry},
            "unchecked_since": unchecked_since,
        }

        def persist() -> None:
            # Like the tracker, log before replacing memory: a retry can duplicate
            # an event after a failed save, but cannot silently lose its history.
            _append_events([event])
            with EVENTS_FILE.open("rb") as log:
                os.fsync(log.fileno())
            _save_alarm_memory(memory)

        try:
            await asyncio.to_thread(persist)
        except OSError:
            logger.exception("Alarm retirement could not be saved")
            return Response(
                content=json.dumps(
                    {
                        "error": "Could not save retirement; the alarm is still held. Retry after checking storage."
                    }
                ),
                status_code=500,
                media_type="application/json",
            )
        _alarm_memory = memory
        _alarm_memory_saved = now
        return Response(
            content=json.dumps({"status": "retired", "source": source, "key": key}),
            media_type="application/json",
        )


@APP.post("/api/alarms/retire")
async def retire_alarm(request: Request) -> Response:
    """Retire a specific held occurrence with the existing dashboard password."""
    if not PASSWORD_FILE.exists():
        return Response(
            content=json.dumps({"error": "No password configured on server"}),
            status_code=503,
            media_type="application/json",
        )
    try:
        body: Any = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("password"), str):
        return Response(
            content=json.dumps(
                {"error": "Expected a JSON object with a password string"}
            ),
            status_code=400,
            media_type="application/json",
        )
    denied: Response | None = await _verify_password_throttled(
        request, body["password"]
    )
    if denied is not None:
        return denied
    source: Any = body.get("source")
    key: Any = body.get("key")
    since: Any = body.get("since")
    unchecked_since: Any = body.get("unchecked_since")
    if (
        not isinstance(source, str)
        or source not in ALARM_SOURCES
        or not isinstance(key, str)
        or not key
        or not _epoch(since)
        or not _epoch(unchecked_since)
    ):
        return Response(
            content=json.dumps(
                {
                    "error": "source, key, since and unchecked_since must identify a held alarm"
                }
            ),
            status_code=400,
            media_type="application/json",
        )
    # A disconnected caller must not release the update lock while a worker is
    # still replacing memory on disk. Complete the operation before cancelling.
    operation: asyncio.Task[Response] = asyncio.create_task(
        _retire_held_alarm(source, key, since, unchecked_since)
    )
    return await _finish_alarm_update(operation)


# The Glances plugins the UI reads, the only ones the /api proxy forwards. An
# allowlist, not a deny-list: Glances decodes and normalizes the path again, so
# any blocked view (/4/processlist, /4/all: full command lines) has encoded
# spellings (process%256cist, /api/../api/4/...) a deny-list check never sees.
GLANCES_PROXY_PLUGINS: Final[tuple[str, ...]] = (
    "cpu",
    "percpu",
    "mem",
    "memswap",
    "gpu",
    "network",
    "diskio",
    "load",
    "uptime",
    "system",
    "fs",
)


@APP.get("/api/version")
async def version() -> Response:
    """The running release and, on a git checkout, the commit it was installed from."""
    return Response(
        content=json.dumps({"version": VERSION, "build": BUILD}),
        media_type="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@APP.get("/api/list-endpoints")
async def list_endpoints() -> Response:
    """Return list of all API endpoints this server implements."""
    endpoints: list[str] = [
        *(f"/4/{plugin}" for plugin in GLANCES_PROXY_PLUGINS),
        "/4/sensors",
        "/cpu-info",
        "/gpu-info",
        "/ip-info",
        "/ping",
        "/gpu-processes",
        "/disk-processes",
        "/network-processes",
        "/processlist",
        "/history",
        "/history/settings",
        "/history/record",
        "/history/field/temps",
        "/history/field/processes",
        "/mail",
        "/storage-health",
        "/sensors",
        "/alarms",
        "/alarms/retire",
        "/services",
        "/services/history",
        "/version",
        "/list-endpoints",
    ]
    return Response(
        content=json.dumps({"endpoints": endpoints, "backend": "python"}),
        media_type="application/json",
    )


@APP.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(path: str, request: Request) -> Response:
    """Proxy the UI's Glances plugins (GLANCES_PROXY_PLUGINS) to Glances.

    Exact match on the decoded path, so what is forwarded is one of the allowed
    literals. Everything else is 404, including Glances' raw process views
    (full command lines); /api/processlist is the sanitized route.
    """
    if not path.startswith("4/") or path[2:] not in GLANCES_PROXY_PLUGINS:
        return Response(status_code=404)
    glances_url: str = request.app.state.glances_url
    client: httpx.AsyncClient
    async with _glances_client() as client:
        url: str = f"{glances_url}/api/{path}"
        error: httpx.RequestError
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
    (0, 0, 0),
    (205, 49, 49),
    (13, 188, 121),
    (229, 229, 16),
    (36, 114, 200),
    (188, 63, 188),
    (17, 168, 205),
    (229, 229, 229),
]


def _ansi_16(r: int, g: int, b: int) -> int:
    """Nearest of the 8 base colours (30-37), bright (90-97) when it's light."""
    i: int = min(
        range(8),
        key=lambda k: sum((a - c) ** 2 for a, c in zip((r, g, b), _ANSI_16[k])),
    )
    return (90 if max(r, g, b) > 191 else 30) + i


ColorDepth: TypeAlias = bool | Literal["24", "256", "16"]


def _paint(text: str, key: str, on: ColorDepth) -> str:
    """Colour `text` with TEXT_COLORS[key]. `on` is the colour depth: False for
    none, True or "24" for 24-bit, "256" or "16" for older terminals."""
    if not on or not text:
        return text
    h: str = TEXT_COLORS[key].lstrip("#")
    r: int
    g: int
    b: int
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    bold: str = "1;" if key == "title" else ""
    if on == "256":
        return f"\033[{bold}38;5;{_ansi_256(r, g, b)}m{text}\033[0m"
    if on == "16":
        return f"\033[{bold}{_ansi_16(r, g, b)}m{text}\033[0m"
    return f"\033[{bold}38;2;{r};{g};{b}m{text}\033[0m"


def _fmt(value: object, spec: str = ".1f", none: str = "-") -> str:
    v: float | None = _num(value)
    return none if v is None else format(v, spec)


def _bar(
    percent: object,
    unicode: bool,
    key: str = "cpu",
    color: ColorDepth = False,
    width: int = TEXT_BAR_WIDTH,
) -> str:
    p: float | None = _num(percent)
    filled: int = 0 if p is None else round(max(0.0, min(p, 100.0)) / 100 * width)
    if unicode:
        return _paint("█" * filled, key, color) + _paint(
            "░" * (width - filled), "muted", color
        )
    return "[" + _paint("#" * filled, key, color) + " " * (width - filled) + "]"


def _range_bar(
    lo: object,
    mid: object,
    hi: object,
    unicode: bool,
    key: str,
    color: ColorDepth,
    width: int = TEXT_BAR_WIDTH,
) -> str:
    """A 0-100 track with min..max filled and the median marked."""
    vals: list[float] = [v for v in (_num(lo), _num(mid), _num(hi)) if v is not None]
    if len(vals) < 3:
        return _bar(None, unicode, key, color, width)
    a: int
    m: int
    b: int
    a, m, b = (round(max(0.0, min(v, 100.0)) / 100 * (width - 1)) for v in vals)
    track: str
    span: str
    mark: str
    track, span, mark = ("░", "▬", "◆") if unicode else (" ", "-", "|")
    cells: list[str] = []
    i: int
    for i in range(width):
        if i == m:
            cells.append(_paint(mark, key, color))
        elif a <= i <= b:
            cells.append(_paint(span, key, color))
        else:
            cells.append(_paint(track, "muted", color))
    body: str = "".join(cells)
    return body if unicode else f"[{body}]"


def _sparkline(
    values: list[float | None],
    chars: str = _SPARK_CHARS,
    lo: float | None = None,
    hi: float | None = None,
) -> str:
    """One character per value on the scale lo..hi (default: the data's own
    min..max); values outside it are clamped."""
    nums: list[float] = [v for v in values if v is not None]
    if not nums:
        return ""
    lo = min(nums) if lo is None else lo
    hi = max(nums) if hi is None else hi
    span: float = (hi - lo) or 1.0
    top: int = len(chars) - 1
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
    threshold: object  # orange-card threshold, or None
    scale: Literal["pct", "zero", "load", "temp"]
    lows: list[float | None] | None = None  # per-record burst minimum, when kept
    peaks: list[float | None] | None = None  # per-record burst maximum, when kept

    def bounds(self: _HistRow) -> tuple[float, float]:
        """The sparkline scale: fixed for percentages, from 0 for rates, so a
        72-74% memory wobble doesn't fill the whole height."""
        # Scale to the averages actually drawn, not burst peaks: one 60 MB/s
        # spike would otherwise flatten every other column.
        top: float = max([v for v in self.values if v is not None], default=0.0)
        if self.scale == "pct":
            return 0.0, 100.0
        if self.scale == "load":
            return 0.0, max(float(os.cpu_count() or 1), top)
        if self.scale == "temp":
            thr: float = _num(self.threshold) or 60.0
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
    span: float = (end - start) / width or 1.0
    sums: list[float] = [0.0] * width
    counts: list[int] = [0] * width
    t: float | None
    v: float | None
    for t, v in zip(times, values):
        if t is None or v is None or not start <= t <= end:
            continue
        b: int = min(width - 1, int((t - start) / span))
        sums[b] += v
        counts[b] += 1
    out: list[float | None] = []
    last_v: float | None = None
    last_t: float = -math.inf
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
    return [
        v
        for v in (_num(t) for t in (record.get("temps") or {}).values())
        if v is not None
    ]


def _record_time(record: dict[str, Any]) -> datetime | None:
    try:
        ts: str = str(record.get("timestamp", "")).replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone()
    except ValueError:
        return None


def _age(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "?"
    sec: int = max(0, int((now - then).total_seconds()))
    if sec < 120:
        return f"{sec}s ago"
    if sec < 7200:
        return f"{sec // 60}m ago"
    return f"{sec / 3600:.1f}h ago"


def _get(record: dict[str, Any], *path: str) -> Any:
    cur: Any = record
    key: str
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _gpu_field(record: dict[str, Any], i: int, field: str) -> Any:
    gpus: list[dict[str, Any]] = record.get("gpus") or []
    return gpus[i].get(field) if i < len(gpus) and isinstance(gpus[i], dict) else None


def _section(
    name: str,
    unicode: bool,
    color: ColorDepth,
    extra: str = "",
    width: int = TEXT_WIDTH,
) -> str:
    dash: str = "─" if unicode else "-"
    head: str = f"{dash * 2} {_paint(name, 'title', color)} "
    tail: str = f" {extra}" if extra else ""
    pad: int = max(2, width - len(name) - 4 - len(_strip_ansi(tail)))
    return head + _paint(dash * pad, "muted", color) + tail


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _level(value: object, threshold: object, key: str) -> str:
    """The value's own colour, or the warning colour at/over the card threshold."""
    v: float | None
    t: float | None
    v, t = _num(value), _num(threshold)
    return "warning" if v is not None and t is not None and v >= t else key


def _str_map(key: str, value_types: tuple[type, ...]) -> dict[str, Any]:
    """A config.json object of name -> value, or {} (with a warning) when it is
    not one. Entries of the wrong type are dropped."""
    raw: Any = _CONFIG.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("config.json: %s must be an object; ignored", key)
        return {}
    return {
        k: v
        for k, v in raw.items()
        if isinstance(v, value_types) and not isinstance(v, bool) and v != ""
    }


# Display names for sensors ({"loc1": "I350 NIC"}); the dashboard applies them,
# records keep the sensor's own name. Here they let a temp_thresholds entry or a
# fans rule be written under either name.
# config.json sensor_labels names any sensor (temperature, fan, power); temp_labels is
# its older name, still read. sensor_labels wins where both name the same sensor.
TEMP_LABELS: Final[dict[str, str]] = {
    **_str_map("temp_labels", (str,)),
    **_str_map("sensor_labels", (str,)),
}
# A sensor's own limit in °C, for sensors that run hot by design.
TEMP_THRESHOLDS: Final[dict[str, float]] = _str_map("temp_thresholds", (int, float))
# config.json sensor_hide, read at startup like the other sensor keys.
_SENSOR_HIDE = _sensor_hide_patterns()


def _temp_limit(label: str, default: float) -> float:
    """The sensor's own limit (temp_thresholds, by its own name or its
    sensor_labels / temp_labels name), else ``default``."""
    name: str | None
    for name in (label, TEMP_LABELS.get(label)):
        if name is not None and name in TEMP_THRESHOLDS:
            return float(TEMP_THRESHOLDS[name])
    return default


def _worst_temp(record: dict[str, Any], threshold: object) -> float | None:
    """The hottest reading measured against each sensor's own limit, on the
    global threshold's scale: a sensor 20 °C under its own 95 °C limit counts as
    20 °C under the global one. None without readings."""
    base: float = _num(threshold) or 60.0
    shifted: list[float] = [
        v - (_temp_limit(label, base) - base)
        for label, v in (
            (label, _num(t)) for label, t in (record.get("temps") or {}).items()
        )
        if v is not None
    ]
    return max(shifted) if shifted else None


def _temp_key(value: object, threshold: object) -> str:
    v: float | None
    t: float
    v, t = _num(value), _num(threshold) or 60.0
    if v is None:
        return "muted"
    return "ok" if v < t - 10 else "temp_mid" if v < t else "error"


def _render_latest(
    record: dict[str, Any],
    unicode: bool,
    now: datetime,
    color: ColorDepth = False,
    thresholds: dict[str, Any] | None = None,
    width: int = TEXT_WIDTH,
    services: Sequence[dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """Snapshot lines as (slot, line). The slot names the history sparkline that
    belongs beside the line in two-column mode ("" for none). ``services`` is
    services_snapshot()'s targets list: it is live state, not part of a record."""
    th: dict[str, Any] = thresholds or {}
    lines: list[tuple[str, str]] = []

    def add(line: str, slot: str = "") -> None:
        lines.append((slot, line))

    def sec(name: str, extra: str = "") -> str:
        return _section(name, unicode, color, extra, width)

    then: datetime | None = _record_time(record)
    when: str = then.strftime("%Y-%m-%d %H:%M:%S %Z") if then else "?"
    deg: str = "°C" if unicode else " C"
    add(
        sec("Snapshot", f"{when} " + _paint(f"({_age(then, now)})", "muted", color)),
        "@header",
    )

    def row(
        label: str,
        percent: object,
        detail: str,
        key: str,
        thr: object = None,
        slot: str = "",
    ) -> None:
        pct: str = _paint(f"{_fmt(percent):>5}%", _level(percent, thr, key), color)
        add(
            _paint(f"{label:<10}", key, color)
            + f" {_bar(percent, unicode, key, color)} {pct}  {detail}",
            slot,
        )

    def gb(m: dict[str, Any], used: str = "used_gb", total: str = "total_gb") -> str:
        return _paint(f"{_fmt(m.get(used))} / {_fmt(m.get(total))} GB", "muted", color)

    cpu: Any = record.get("cpu") or {}
    burst: float = _num(record.get("duration_sec")) or 0
    row(
        "CPU",
        cpu.get("avg"),
        _paint(f"max {_fmt(cpu.get('max'))}%", "muted", color) if burst else "",
        "cpu",
        th.get("cpu_percent"),
        "cpu",
    )
    spread: Any = record.get("cpu_cores") or {}
    if spread:
        lo: Any
        med: Any
        hi: Any
        lo, med, hi = (spread.get(k) for k in ("min", "median", "max"))
        add(
            _paint(f"{'  cores':<10}", "cpu_cores", color)
            + f" {_range_bar(lo, med, hi, unicode, 'cpu_cores', color)} "
            + _paint(f"{_fmt(med):>5}%", "cpu_cores", color)
            + _paint(f"  median, range {_fmt(lo)}-{_fmt(hi)}%", "muted", color),
            "cpu_cores",
        )

    load: Any = record.get("load") or {}
    cores: int = os.cpu_count() or 1
    load1: float | None = _num(load.get("min1"))
    load_pct: None | float = None if load1 is None else 100 * load1 / cores
    row(
        "Load",
        load_pct,
        "  ".join(
            _paint(f"{label} {_fmt(load.get(k), '.2f')}", key, color)
            for label, k, key in (
                ("1m", "min1", "load"),
                ("5m", "min5", "load_5m"),
                ("15m", "min15", "load_15m"),
            )
        )
        + _paint(f"  / {cores} cores", "muted", color),
        "load",
        slot="load",
    )
    key: str
    label: str
    for key, label in (("memory", "Memory"), ("swap", "Swap")):
        m: Any = record.get(key) or {}
        row(label, m.get("percent"), gb(m), key, th.get(f"{key}_percent"), key)

    temps: Any = record.get("temps") or {}
    gpus: list[dict[str, Any]] = record.get("gpus") or []
    if gpus:
        add(sec("GPUs"))
    i: int
    gpu: dict[str, Any]
    for i, gpu in enumerate(gpus):
        idx: Any = gpu.get("index")
        t: Any = temps.get(f"GPU {idx}")
        temp: str = (
            ""
            if t is None
            else _paint(
                f"{_fmt(t, '.0f')}{deg}",
                _temp_key(
                    t, _temp_limit(f"GPU {idx}", _num(th.get("temp_celsius")) or 60.0)
                ),
                color,
            )
        )
        row(
            f"GPU {idx}",
            gpu.get("util_percent"),
            temp,
            "gpu",
            th.get("gpu_util_percent"),
            f"gpu{i}",
        )
        row(
            "  memory",
            gpu.get("mem_percent"),
            gb(gpu, "mem_used_gb", "mem_total_gb"),
            "gpu_mem",
            th.get("gpu_memory_percent"),
            f"gmem{i}",
        )

    add(sec("I/O"))
    up: str
    down: str
    up, down = ("↑", "↓") if unicode else ("^", "v")
    net_thr: Any
    io_thr: Any
    net_thr, io_thr = th.get("network_mbps"), th.get("disk_io_mbps")
    io: list[tuple[str, str, str, Any, str, Any]] = [
        (
            "Network",
            down,
            "in",
            _get(record, "network", "in_mbps", "avg"),
            "net_in",
            net_thr,
        ),
        (
            "Network",
            up,
            "out",
            _get(record, "network", "out_mbps", "avg"),
            "net_out",
            net_thr,
        ),
        (
            "Disk I/O",
            up,
            "read",
            _get(record, "disk_io", "read_mbps", "avg"),
            "read",
            io_thr,
        ),
        (
            "Disk I/O",
            down,
            "write",
            _get(record, "disk_io", "write_mbps", "avg"),
            "write",
            io_thr,
        ),
    ]

    def io_cell(arrow: str, name: str, v: object, k: str, thr: object) -> str:
        return _paint(f"{arrow} {name:<5}", k, color) + _paint(
            f"{_fmt(v, '.2f'):>8}", _level(v, thr, k), color
        )

    pair: list[tuple[str, str, str, Any, str, Any]]
    for pair in (io[:2], io[2:]):
        add(
            _paint(f"{pair[0][0]:<10}", pair[0][4], color)
            + " "
            + "    ".join(io_cell(*c[1:]) for c in pair)
            + _paint("  MB/s", "muted", color),
            pair[0][4],
        )

    pings: list[tuple[str, dict[str, Any]]] = [
        (t, p) for t, p in (record.get("ping") or {}).items() if isinstance(p, dict)
    ]
    cells: list[str] = []
    target: Any
    p: dict[str, Any]
    for i, (target, p) in enumerate(pings):
        key = f"ping_{i % 3}"
        avg: float | None
        loss: float
        avg, loss = _num(p.get("avg")), _num(p.get("loss")) or 0.0
        # Target's colour on the marker, the address muted, the measurement in
        # the target's colour (orange over the threshold), so they read apart.
        dot: str = (
            _paint("●" if unicode else "*", key, color)
            + " "
            + _paint(target, "muted", color)
            + " "
        )
        if avg is None:
            cells.append(dot + _paint("DOWN", "error", color))
            continue
        cell: str = dot + _paint(
            f"{avg:.1f} ms", _level(avg, th.get("ping_ms"), key), color
        )
        if loss > 0:
            cell += _paint(f" {loss * 100:.0f}% loss", "warning", color)
        cells.append(cell)
    if cells:
        add(
            _paint(f"{'Ping':<10}", "ping", color) + " " + "   ".join(cells),
            f"ping:{pings[0][0]}",
        )

    # Services: up or down at the last check and the 24 h uptime, wrapped to
    # the width (names are free text and there may be many).
    service_cells: list[str] = []
    service: dict[str, Any]
    for service in services or []:
        ok: Any = service.get("ok")
        day: dict[str, Any] = (service.get("uptime") or {}).get("24") or {}
        state: str = (
            _paint("?", "muted", color)
            if ok is None
            else _paint("UP", "ok", color)
            if ok
            else _paint("DOWN", "error", color)
        )
        share: str = (
            f" {100 * day['up'] / day['checks']:.1f}%" if day.get("checks") else ""
        )
        service_cells.append(
            _paint(
                "●" if unicode else "*",
                # Not checked yet or stale is unknown, not down.
                "muted" if ok is None else "ok" if ok else "error",
                color,
            )
            + " "
            + _paint(str(service.get("name")), "muted", color)
            + " "
            + state
            + _paint(share, "muted", color)
        )
    service_line: str = _paint(f"{'Services':<10}", "ok", color)
    on_line: int = 0
    for cell in service_cells:
        if on_line and len(_strip_ansi(service_line + cell)) + 3 > width:
            add(service_line)
            service_line, on_line = " " * 10, 0
        service_line += (" " if not on_line else "   ") + cell
        on_line += 1
    if on_line:
        add(service_line)

    readings: list[float] = _temps_of(record)
    if readings:
        add(sec(f"Temperatures ({deg.strip()})", f"{len(readings)} sensors"))
        thr: Any = th.get("temp_celsius")
        # "max" takes its colour from the sensor nearest its own limit
        # (temp_thresholds), which need not be the hottest one.
        add(
            "   ".join(
                _paint(f"{label} ", "muted", color)
                + _paint(
                    f"{value:>3.0f}",
                    _temp_key(
                        _worst_temp(record, thr) if label == "max" else value, thr
                    ),
                    color,
                )
                for label, value in (
                    ("min", min(readings)),
                    ("median", statistics.median(readings)),
                    ("mean", statistics.fmean(readings)),
                    ("max", max(readings)),
                )
            ),
            "maxt",
        )

    drives: list[dict[str, Any]] = record.get("drives") or []
    if drives:
        add(sec("Drives"))
    d: Any
    for d in drives:
        mount: str = str(d.get("mount", "?"))
        row(
            mount
            if len(mount) <= 10
            else "…" + mount[-9:]
            if unicode
            else ".." + mount[-8:],
            d.get("percent"),
            gb(d),
            "drive",
            th.get("disk_percent"),
        )
    return lines


TEXT_STATS_WIDTH: Final[int] = 23  # "    min    mean     max"
TEXT_AUTO_HOURS: Final[tuple[float, float]] = (1.0, 3.0)


def _text_layout(width: int, view: str, unicode: bool) -> tuple[int, bool, int]:
    """(width, two-column?, column width) for a requested report width."""
    width = max(TEXT_MIN_WIDTH, width)
    two_col: bool = view == "both" and unicode and width >= TEXT_TWO_COLUMN_WIDTH
    return width, two_col, (width - 3) // 2 if two_col else width


def _spark_field_width(col_w: int) -> int:
    return max(10, col_w - 8 - 2 - TEXT_STATS_WIDTH)


def auto_history_hours(
    width: int, view: str, unicode: bool, record_interval: float
) -> float:
    """Default window: as many records as the sparklines have columns, so each
    column is about one record, kept between 1 and 3 hours."""
    col_w: int
    _, _, col_w = _text_layout(width, view, unicode)
    span: float = _spark_field_width(col_w) * max(1.0, record_interval) / 3600
    lo: float
    hi: float
    lo, hi = TEXT_AUTO_HOURS
    return round(min(hi, max(lo, span)), 2)


def _render_history(
    records: list[dict[str, Any]],
    unicode: bool,
    hours: float,
    rows: int,
    color: ColorDepth = False,
    thresholds: dict[str, Any] | None = None,
    table: bool = False,
    width: int = TEXT_WIDTH,
    now: datetime | None = None,
    sparks: bool = True,
) -> tuple[str, list[tuple[str, str]], list[str], list[str]]:
    """(section header, sparkline rows as (slot, line), footer, table lines).

    Rows follow the snapshot's order; slots match _render_latest's so two-column
    mode can pair each row with its snapshot line. The footer is the time axis
    and legend. sparks=False leaves out the sparklines and axis (a requested
    table that wouldn't fit otherwise takes their place).
    """
    th: dict[str, Any] = thresholds or {}
    labels: str = f"{'min':>7} {'mean':>7} {'max':>7}"
    header: str = _section(
        f"History, last {hours:g}h ({len(records)} records)",
        unicode,
        color,
        _paint(labels, "muted", color),
        width,
    )
    if not records:
        return header, [], [_paint("No history records yet.", "muted", color)], []
    n_gpus: int = max((len(r.get("gpus") or []) for r in records), default=0)

    def series(fn: Callable[[dict[str, Any]], object]) -> list[float | None]:
        return [_num(fn(r)) for r in records]

    def avg_min_max(*path: str) -> dict[str, list[float | None]]:
        return {
            "values": series(lambda r: _get(r, *path, "avg")),
            "lows": series(lambda r: _get(r, *path, "min")),
            "peaks": series(lambda r: _get(r, *path, "max")),
        }

    columns: list[_HistRow] = [
        _HistRow(
            "cpu",
            "CPU%",
            spec=".1f",
            key="cpu",
            threshold=th.get("cpu_percent"),
            scale="pct",
            **avg_min_max("cpu"),
        ),
        _HistRow(
            "cpu_cores",
            "Cores",
            series(lambda r: _get(r, "cpu_cores", "median")),
            ".1f",
            "cpu_cores",
            None,
            "pct",
        ),
        _HistRow(
            "load",
            "Load1",
            series(lambda r: _get(r, "load", "min1")),
            ".2f",
            "load",
            None,
            "load",
        ),
        _HistRow(
            "memory",
            "Mem%",
            series(lambda r: _get(r, "memory", "percent")),
            ".1f",
            "memory",
            th.get("memory_percent"),
            "pct",
        ),
        _HistRow(
            "swap",
            "Swap%",
            series(lambda r: _get(r, "swap", "percent")),
            ".1f",
            "swap",
            th.get("swap_percent"),
            "pct",
        ),
    ]
    i: int
    for i in range(n_gpus):
        columns.append(
            _HistRow(
                f"gpu{i}",
                f"GPU{i}%",
                series(lambda r, i=i: _gpu_field(r, i, "util_percent")),
                ".0f",
                "gpu",
                th.get("gpu_util_percent"),
                "pct",
            )
        )
        columns.append(
            _HistRow(
                f"gmem{i}",
                f"GMem{i}",
                series(lambda r, i=i: _gpu_field(r, i, "mem_percent")),
                ".0f",
                "gpu_mem",
                th.get("gpu_memory_percent"),
                "pct",
            )
        )
    slot: str
    name: str
    path: tuple[str, str]
    key: str
    thr: Any
    for slot, name, path, key, thr in (
        ("net_in", "NetIn", ("network", "in_mbps"), "net_in", th.get("network_mbps")),
        (
            "net_out",
            "NetOut",
            ("network", "out_mbps"),
            "net_out",
            th.get("network_mbps"),
        ),
        ("read", "DiskR", ("disk_io", "read_mbps"), "read", th.get("disk_io_mbps")),
        ("write", "DiskW", ("disk_io", "write_mbps"), "write", th.get("disk_io_mbps")),
    ):
        columns.append(
            _HistRow(
                slot,
                name,
                spec=".2f",
                key=key,
                threshold=thr,
                scale="zero",
                **avg_min_max(*path),
            )
        )
    # The hottest sensor in each record.
    columns.append(
        _HistRow(
            "maxt",
            "MaxT",
            [max(t) if t else None for t in map(_temps_of, records)],
            ".0f",
            "temp",
            th.get("temp_celsius"),
            "temp",
        )
    )

    # Sparkline field fills the width so the stats line up under the header labels.
    field_w: int = _spark_field_width(width)
    spark_w: int = field_w
    # Real time axis: the window ends now and spans `hours`.
    end_dt: datetime = now or datetime.now(timezone.utc).astimezone()
    end: float = end_dt.timestamp()
    start: float = end - hours * 3600
    rec_times: list[float | None] = [
        t.timestamp() if t else None for t in map(_record_time, records)
    ]
    # Hold a value across empty columns for up to 1.5 record intervals.
    interval: float = getattr(
        APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL
    )
    hold: float = 1.5 * max(float(interval), (end - start) / spark_w)
    chars: str = _SPARK_CHARS if unicode else _SPARK_ASCII
    spark_rows: list[tuple[str, str]] = []
    row: _HistRow
    for row in columns:
        vals: list[float | None]
        spec: str
        slot, name, vals, spec, key, thr = row[:6]
        nums: list[float] = [v for v in vals if v is not None]
        # min / max come from the bursts' own extremes when records keep them,
        # so a short spike shows in "max" even though the average hides it.
        lows: list[float] = [v for v in (row.lows or []) if v is not None] or nums
        peaks: list[float] = [v for v in (row.peaks or []) if v is not None] or nums

        def cell(v: float, shade: str) -> str:
            return _paint(f"{format(v, spec):>7}", _level(v, thr, shade), color)

        stats: str = (
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
        spark: str = _paint(
            f"{_sparkline(_time_buckets(rec_times, vals, start, end, spark_w, hold), chars, *row.bounds()):<{field_w}}",
            key,
            color,
        )
        spark_rows.append(
            (slot, f"{_paint(f'{name:<7}', key, color)} {spark}  {stats}")
        )

    # Time axis under the sparklines: window start, middle and end (now).
    fmt: str = "%m-%d %H:%M" if hours > 20 else "%H:%M"

    def label(ts: float) -> str:
        # Resolve local time at each tick: end_dt's fixed offset may be on the
        # other side of a DST transition, unlike the record timestamps below.
        return datetime.fromtimestamp(ts).strftime(fmt)

    if spark_w >= 3:
        rule: str = (
            ("└" + "─" * (spark_w - 2) + "┘")
            if unicode
            else ("+" + "-" * (spark_w - 2) + "+")
        )
    else:
        rule = ("─" if unicode else "-") * spark_w
    first: str
    last: str
    first, last = label(start), label(end)
    ticks: str = first + last.rjust(max(0, spark_w - len(first)))
    mid: str = label((start + end) / 2)
    if spark_w >= len(first) + len(mid) + len(last) + 4:
        at: int = spark_w // 2 - len(mid) // 2
        ticks = ticks[:at] + mid + ticks[at + len(mid) :]
        # A tick on the rule over the middle label's centre.
        centre: int = at + len(mid) // 2
        rule = rule[:centre] + ("┬" if unicode else "+") + rule[centre + 1 :]
    deg: str = "°C" if unicode else "C"
    legend: str = _paint(
        f"Blank = no records. Net/Disk MB/s, MaxT = hottest sensor {deg}.",
        "muted",
        color,
    )
    foot: list[str] = [
        " " * 8 + _paint(rule, "muted", color),
        " " * 8 + _paint(ticks, "muted", color),
        legend,
    ]
    if not sparks:
        spark_rows, foot = [], [legend]
    if not table:
        return header, spark_rows, foot, []

    # Evenly sample at most `rows` records, always including the newest.
    count: int = len(records)
    shown: int = min(rows, count)
    picks: list[int] = (
        [count - 1]
        if shown == 1
        else [round(k * (count - 1) / (shown - 1)) for k in range(shown)]
    )
    sep: str = " │ " if unicode else " | "
    table_w: int = 14 + len(sep) + 7 * len(columns) - 1
    tail: list[str] = [""] if sparks else []
    tail.append(
        f"{'Time':<14}"
        + sep
        + " ".join(_paint(f"{c.name[:6]:>6}", c.key, color) for c in columns)
    )
    tail.append(("─" if unicode else "-") * table_w)
    idx: int
    for idx in picks:
        t: datetime | None = _record_time(records[idx])
        stamp: str = t.strftime("%m-%d %H:%M:%S") if t else "?"
        cells: str = " ".join(
            _paint(f"{_fmt(c.values[idx], c.spec):>6}", "warning", color)
            if _level(c.values[idx], c.threshold, "") == "warning"
            else f"{_fmt(c.values[idx], c.spec):>6}"
            for c in columns
        )
        tail.append(
            _paint(f"{stamp:<14}", "muted", color) + _paint(sep, "muted", color) + cells
        )
    return header, spark_rows, foot, tail


def render_text(
    records: list[dict[str, Any]],
    *,
    style: str = "unicode",
    view: str = "both",
    hours: float = 1,
    rows: int = 24,
    title: str = "",
    color: ColorDepth = False,
    thresholds: dict[str, Any] | None = None,
    table: bool = False,
    live: dict[str, Any] | None = None,
    width: int = TEXT_WIDTH,
    now: datetime | None = None,
    sparks: bool = True,
    services: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Render history records as a console report (pure; unit-tested).

    The snapshot is `live` (a fresh sample) when given, else the newest record.
    """
    unicode: bool = style == "unicode"
    now = now or datetime.now(timezone.utc).astimezone()
    heading: str = title or socket.gethostname()
    host: str = socket.gethostname()
    sub: str = "" if heading == host else (f"· {host}" if unicode else f"- {host}")
    inner: str = f" {heading} " + _paint(sub, "muted", color) + " "
    two_col: bool
    col_w: int
    width, two_col, col_w = _text_layout(width, view, unicode)
    span: int = width - 2
    hint: str = "options and API: /help "
    fill: int = max(1, span - len(_strip_ansi(inner)) - len(hint))
    if unicode:
        out: list[str] = [
            _paint("╭" + "─" * span + "╮", "cpu", color),
            _paint("│", "cpu", color)
            + _paint(inner[: len(heading) + 2], "title", color)
            + inner[len(heading) + 2 :]
            + " " * fill
            + _paint(hint, "muted", color)
            + _paint("│", "cpu", color),
            _paint("╰" + "─" * span + "╯", "cpu", color),
        ]
    else:
        title_line: str = inner.strip()
        out = [
            "=" * width,
            title_line + hint.rstrip().rjust(width - len(_strip_ansi(title_line))),
            "=" * width,
        ]
    left: list[tuple[str, str]] = []
    if view in ("latest", "both"):
        if live or records:
            left = _render_latest(
                live or records[-1], unicode, now, color, thresholds, col_w, services
            )
        else:
            left = [("", "No data yet.")]
    hist: tuple[str, list[tuple[str, str]], list[str], list[str]] | None = None
    if view in ("history", "both"):
        hist = _render_history(
            records, unicode, hours, rows, color, thresholds, table, col_w, now, sparks
        )

    if two_col and hist:
        # Snapshot on the left, history on the right. Each history row goes
        # beside the snapshot line with its slot when that line is still ahead,
        # else on the next free line; the axis and legend follow.
        header: str
        spark_rows: list[tuple[str, str]]
        foot: list[str]
        table_lines: list[str]
        header, spark_rows, foot, table_lines = hist
        index: dict[str, int] = {slot: i for i, (slot, _) in enumerate(left) if slot}
        right: list[str] = [header] + [""] * (len(left) - 1)
        pos: int = 1
        slot: str
        line: str
        for slot, line in spark_rows + [("", t) for t in foot]:
            at: int = index.get(slot, -1)
            at = at if at >= pos else pos
            while at < len(right) and right[at]:
                at += 1
            if at >= len(right):
                right += [""] * (at - len(right) + 1)
            right[at] = line
            pos = at + 1
        extra: list[str] = right[len(left) :]
        rows_left: list[str] = [line for _, line in left] + [""] * len(extra)
        a: str
        b: str
        for a, b in zip(rows_left, right):
            out.append(a + " " * max(0, col_w - len(_strip_ansi(a))) + "   " + b)
        out += table_lines  # the optional table, full width below
    else:
        out += [line for _, line in left]
        if left and hist:
            out.append("")
        if hist:
            header, spark_rows, foot, table_lines = hist
            out += [header] + [line for _, line in spark_rows] + foot + table_lines
    return "\n".join(out) + "\n"


TEXT_USAGE: Final[str] = (
    "usage: /text?view=latest|history|both&style=unicode|ascii|simple&color=auto|on|off"
    "&hours=N&width=80&height=N&live=0|1&table=0|1&rows=24  (see /help)\n"
)

HELP_TEXT: Final[str] = """\
Horatio <version>: system monitor for this machine. Plain-text help, for people and agents.

QUICK START
  curl "http://<host>:8080/text?width=$COLUMNS"   # colour report: live snapshot + history
  curl http://<host>:8080/text?style=simple      # plain ASCII, no colour (best for agents/logs)
  curl -fsS http://<host>:8080/tui.py | python3 -   # live, auto-updating view
  python3 python/tui.py                          # the same, from a horatio checkout on the box

LIVE VIEW (tui.py)
  Standard library only (Python 3.8+, Linux/macOS). Detects terminal size,
  colour depth (NO_COLOR, FORCE_COLOR, COLORTERM, TERM) and UTF-8, and redraws
  every 2 s (--interval). Keys, listed at the bottom: q quit, r refresh,
  v cycle view, t table, +/- history window. --color on|256|16|off and --style override the
  detection; --once prints a single frame. Piping from curl runs whatever the
  server sends: fine on your own network. Otherwise download, read, then run:
    curl -fsS -o tui.py http://<host>:8080/tui.py && python3 tui.py
  From a horatio checkout: python3 python/tui.py on the box itself (finds the local
  server), or python3 python/tui.py http://<host>:8080 (or set HORATIO_URL).

/text  console report built from the history file
  view   both (default) | latest | history
         latest  = a live snapshot, sampled when you request it
         history = per-metric sparklines (incl. max temperature) over the window
  style  unicode (default: bars, sparklines, box drawing)
         | ascii (7-bit characters)
         | simple (ascii with no colour)
  color  auto (default: on for unicode, off for ascii/simple) | on | 256 | 16 | off
         on is 24-bit ANSI; 256/16 for older terminals; turn it off when parsing
  hours  history window (0.1 to the config's history.max_hours, 8760 unless set).
         Default: 1 to 3 hours, picked so the sparklines have about one record
         per column (wider terminals and slower recording intervals get the
         longer window)
  width  terminal width in columns, default 80 (60 to 400). The server can't see
         your terminal, so pass it:  curl "http://<host>:8080/text?width=$COLUMNS"
         At 160 or wider, the unicode report shows snapshot and history side by side.
  height trim to N lines: a requested table shrinks to fit, and if it still
         doesn't fit it replaces the sparklines; anything still over is cut,
         with a note saying how much
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
  - Percentages are 0-100. Network and disk I/O are MB/s (megabytes). Each
    network interface and each drive is capped at config.json's
    limits.network_mbps / limits.disk_io_mbps (defaults 1250 and 15000, read at
    startup); the totals are their sums and are not capped. A record that hit a
    cap lists it under rate_capped. Temperatures are Celsius. Ping is round-trip
    ms; DOWN means every probe was lost.
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
                                 gpus, drives, network, disk_io, load, ping, cpu_freq;
                                 &since=<timestamp, URL-encoded> returns only
                                 records newer than it. Every response has cutoff
                                 (window start) and now (epoch s). Records may carry
                                 rate_capped: [{metric, interface | device, drive,
                                 samples, of, raw_max, cap}] and rate_caps
  GET /api/history/field/temps   [{timestamp, temps}] for the same range (?hours=, &since=)
  GET /api/history/field/processes   per-process rows, when process logging is on
  GET /api/history/settings      recording interval and sampling settings
  GET /api/processlist           live process list (arguments stripped)
  GET /api/gpu-info              live NVIDIA GPU details
  GET /api/gpu-processes         live processes holding GPU memory, with per-GPU
                                 gpu_util_percent / gpu_mem_util_percent (null: no sample)
  GET /api/cpu-info              CPU model and core counts
  GET /api/version               {version, build}: the running release and, on a
                                 git checkout, the commit it was installed from
  GET /api/ping                  ping the configured targets now
  GET /api/services              each http_targets URL: up or down at the last check,
                                 uptime {up, checks} over the last 1 / 24 / 168 h
  GET /api/services/history?hours=24   [passed, failed] checks per target per time bucket
  GET /api/sensors               fans/temps/power snapshot (root collector, when installed)
  GET /api/alarms                everything wrong now in one list, since when, and the
                                 newest alarm changes: {sources, active, held, recent,
                                 notes}
  GET /api/storage-health        drive SMART/RAID/ECC snapshot (root collector)
  GET /api/4/sensors             temperatures, fans, power from hwmon (the server's own)
  GET /api/4/<plugin>            live Glances metrics, raw (rates not capped): cpu,
                                 mem, memswap, load, network, diskio, fs, gpu
  GET /config.json               the config file as written: title, ping targets,
                                 http targets, thresholds, limits if set
  GET /themes/index.json         names of the themes in themes/ (see themes/README.md)

Dashboard: http://<host>:8080/   This page: /help
"""


@APP.get("/text")
async def text_view(
    view: str = "both",
    style: str = "unicode",
    color: str = "auto",
    hours_arg: str | None = Query(None, alias="hours"),
    rows_arg: str | None = Query(None, alias="rows"),
    table: bool = False,
    width_arg: str | None = Query(None, alias="width"),
    height_arg: str | None = Query(None, alias="height"),
    live: bool = True,
) -> Response:
    """Plain-text report for terminals: `curl http://<host>:8080/text`.

    style=simple is ascii without colour. Colour defaults on (24-bit) for
    unicode and off otherwise; color=on|256|16|off overrides. height=N trims the
    report to N lines (the TUI passes its terminal size). live=0 uses the newest
    record instead of a fresh sample. Documented at /help.
    """
    # Parsed here, not by FastAPI: a bad number gets the usage line like every
    # other bad option, and an empty one (width=$COLUMNS with COLUMNS unset) the
    # default.
    try:
        hours: float | None = float(hours_arg) if hours_arg else None
        # int(float()): width=80.0 was accepted before and still is.
        rows: int = int(float(rows_arg)) if rows_arg else 24
        width: int = int(float(width_arg)) if width_arg else TEXT_WIDTH
        height: int | None = int(float(height_arg)) if height_arg else None
    except (ValueError, OverflowError):
        return Response(content=TEXT_USAGE, status_code=400, media_type="text/plain")
    if (
        view not in ("latest", "history", "both")
        or style not in ("ascii", "unicode", "simple")
        or color not in ("auto", "on", "256", "16", "off")
    ):
        return Response(content=TEXT_USAGE, status_code=400, media_type="text/plain")
    if hours is None:
        hours = auto_history_hours(
            width,
            view,
            style == "unicode",
            getattr(
                APP.state, "history_record_interval", DEFAULT_HISTORY_RECORD_INTERVAL
            ),
        )
    hours = _clamp_hours(hours)
    rows = max(1, min(rows, 500))
    requested_rows: int = rows
    result: tuple[list[dict[str, Any]], int, bool, float] | str = await _history_result(
        hours, None
    )
    if isinstance(result, str):
        return Response(
            content=f"Failed to read history: {result}\n",
            status_code=500,
            media_type="text/plain",
        )
    records: list[dict[str, Any]]
    _skipped: int
    _truncated: bool
    _cutoff: float
    records, _skipped, _truncated, _cutoff = result
    sample: dict[str, Any] | None = None
    if live and view in ("latest", "both"):
        e: Exception
        try:
            sample = await _live_sample()
        except Exception as e:
            logger.warning("/text: live sample failed, using the newest record: %s", e)
    if style == "simple":
        style, color = "ascii", "off"
    # Capture service state before rendering; uptime counting runs in a worker.
    services_now: list[dict[str, Any]] = []
    if view != "history":
        snapshot: dict[str, Any] = await services_snapshot(time.time())
        # Stale results draw as "?", not as the last UP or DOWN.
        services_now = [
            {**t, "ok": None} if snapshot["stale"] else t for t in snapshot["targets"]
        ]
    depths: dict[str, ColorDepth] = {"on": "24", "256": "256", "16": "16", "off": False}
    depth: ColorDepth = depths.get(color, "24" if style == "unicode" else False)

    def render(with_table: bool, sparks: bool = True) -> str:
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
            sparks=sparks,
            services=services_now,
        )

    def layout() -> str:
        nonlocal rows, height
        body: str = render(table)
        if height is not None:
            height = max(5, height)
            if table and body.count("\n") > height:
                # Asked for the table: shrink it to the rows that fit (at least 3)
                # before cutting. It shows min(rows, records) rows, so shrink that.
                shown: int = min(rows, len(records))
                rows = max(3, shown - (body.count("\n") - height))
                body = render(True)
                if body.count("\n") > height:
                    # Still too tall: the table replaces the sparklines, then fills
                    # whatever room that frees.
                    body = render(True, sparks=False)
                    over: int = body.count("\n") - height  # negative = spare lines
                    rows = max(3, min(requested_rows, min(rows, len(records)) - over))
                    body = render(True, sparks=False)
            body = _fit_height(body, height, depth)
        return body

    # A long window takes a second or more to render: keep it off the event loop.
    body: str = await asyncio.to_thread(layout)
    return Response(content=body, media_type="text/plain; charset=utf-8")


def _fit_height(body: str, height: int, color: ColorDepth) -> str:
    """Trim a report to `height` lines, noting how many were cut."""
    lines: list[str] = body.rstrip("\n").split("\n")
    if len(lines) <= height:
        return body
    cut: int = len(lines) - (height - 1)
    return (
        "\n".join(
            lines[: height - 1]
            + [
                _paint(
                    f"... {cut} more lines (taller terminal, or another view)",
                    "muted",
                    color,
                )
            ]
        )
        + "\n"
    )


# Short-lived caches so several terminals polling /text every second cost one
# live sample per second and one history parse per new record.
_LIVE_TTL: Final[float] = 1.0
# One history parse at a time: a year-long range over a large file parses up to
# HISTORY_MAX_TAIL_READ of JSON (hundreds of MB in Python objects); several at
# once could exhaust a small box. A request the cached window answers does not
# take the lock.
_history_read_lock: asyncio.Lock = asyncio.Lock()
# The last parsed window, shared by /api/history, /api/history/field and /text:
# a page opening several history cards, its since= top-ups and every terminal
# are answered from one parse until the next record is appended.
_history_cache: dict[str, _HistoryWindow | None] = {"window": None}
# A window spanning more of the file than this is served but not kept: parsed
# records take several times their size on disk.
HISTORY_CACHE_MAX_WINDOW: Final[int] = 8 * 1024 * 1024


async def _live_sample() -> dict[str, Any]:
    return await _cached(
        "live",
        _LIVE_TTL,
        lambda: collect_history_sample(APP.state.glances_url, live=True),
    )


def _history_from_cache(
    hours: float, since: float | None
) -> tuple[list[dict[str, Any]], int, bool, float] | None:
    """A history request answered from the cached window, or None when the file
    has changed since it was parsed or the window does not reach far enough back
    (sync: one stat)."""
    window: _HistoryWindow | None = _history_cache["window"]
    if window is None:
        return None
    try:
        st: os.stat_result = HISTORY_FILE.stat()
    except OSError:
        return None
    if (st.st_mtime_ns, st.st_size) != window.stat:
        return None
    cutoff: float = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    if not (window.hit_cap or _history_window_covers(window, cutoff, since)):
        return None
    return _serve_history_window(window, cutoff, since)


def _read_history_cached(
    hours: float, since: float | None = None
) -> tuple[list[dict[str, Any]], int, bool, float] | str:
    """_read_history through the cached window: the file is parsed only when it
    has changed or the request reaches further back than the window (sync).
    Callers must not mutate the returned records. ``skipped`` counts the whole
    window's malformed lines, which may span more than this request's range."""
    hit: tuple[list[dict[str, Any]], int, bool, float] | None = _history_from_cache(
        hours, since
    )
    if hit is not None:
        return hit
    cutoff: float = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    if not HISTORY_FILE.exists():
        return [], 0, False, cutoff
    e: Exception
    try:
        window: _HistoryWindow = _read_history_window(cutoff, since)
    except Exception as e:
        logger.error("Failed to read history file: %s", e)
        return str(e)
    _history_cache["window"] = (
        window if window.size <= HISTORY_CACHE_MAX_WINDOW else None
    )
    return _serve_history_window(window, cutoff, since)


async def _history_result(
    hours: float, since: float | None
) -> tuple[list[dict[str, Any]], int, bool, float] | str:
    """A history read for a request: from the cached window when it answers the
    request, else one parse at a time (see _history_read_lock)."""
    hit: tuple[list[dict[str, Any]], int, bool, float] | None = await asyncio.to_thread(
        _history_from_cache, hours, since
    )
    if hit is not None:
        return hit
    async with _history_read_lock:
        return await asyncio.to_thread(_read_history_cached, hours, since=since)


@APP.get("/tui.py")
async def tui_script(request: Request) -> Response:
    """The live terminal client, so it runs without a checkout:
    curl -fsS http://<host>:8080/tui.py | python3 -
    The copy served has SERVED_URL set to the address it was fetched from, so
    it connects back here with no argument."""
    source: str = (SOURCE_DIR / "tui.py").read_text()
    base: str = _base_url(request)
    source = source.replace('SERVED_URL: str = ""', f"SERVED_URL: str = {base!r}", 1)
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
    body: str = HELP_TEXT.replace("http://<host>:8080", _base_url(request)).replace(
        "<version>", f"v{VERSION}", 1
    )
    return Response(content=body, media_type="text/plain; charset=utf-8")


@APP.get("/")
async def index() -> FileResponse:
    """Serve index.html, revalidated on every load (see _RevalidatedStaticFiles)."""
    return FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


@APP.get("/config.json")
async def config_json() -> FileResponse:
    """Serve dashboard configuration."""
    return FileResponse(CONFIG_FILE)


def _theme_names() -> list[str]:
    """Stems of themes/*.css that the page can load, sorted; [] without the dir."""
    return sorted(
        path.stem
        for path in THEMES_DIR.glob("*.css")
        if THEME_NAME_RE.fullmatch(path.stem) and path.stem != "default"
    )


@APP.get("/themes/index.json")
async def theme_index() -> Response:
    """The theme picker's options. Not cached, so a newly added theme shows up."""
    return Response(
        content=json.dumps(_theme_names()),
        media_type="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@APP.get("/themes/{name}.css")
async def theme_css(name: str) -> Response:
    """One theme stylesheet. The name rule rules out traversal; no-cache so an
    edited theme shows on a plain reload."""
    if name not in _theme_names() or not (THEMES_DIR / f"{name}.css").is_file():
        return Response(status_code=404)
    return FileResponse(
        THEMES_DIR / f"{name}.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


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
    sock: socket.socket
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
        sock: socket.socket
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


class Arguments(argparse.Namespace):
    port: int
    glances_port: int
    host: str
    set_password: bool


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
    args: Arguments = parser.parse_args(namespace=Arguments())

    if args.set_password:
        pw: str = getpass.getpass("Enter new password: ")
        pw2: str = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Passwords do not match.")
            sys.exit(1)
        PASSWORD_FILE.write_text(_hash_password(pw) + "\n")
        # Restrict permissions — hash is sensitive (offline brute-force target)
        PASSWORD_FILE.chmod(0o600)
        print(f"Password saved to {PASSWORD_FILE}")
        sys.exit(0)

    main(port=args.port, glances_port=args.glances_port, host=args.host)
