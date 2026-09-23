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
from typing import Any, Final

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
DEFAULT_MANUAL_RECORD_MIN_INTERVAL: Final[int] = 60  # seconds
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

    except (OSError, PermissionError, FileNotFoundError, IndexError) as e:
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
    """Parse nvidia-smi --query-gpu CSV (index,util,mem.used,mem.total,temp,power).

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
                "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
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
    size AND used all match; among those we keep the shortest real mount path
    ('/' over '/tmp', non-empty preferred, lexicographic tie-break). The size/used
    check keeps genuinely distinct same-device mounts (btrfs subvolumes etc.)
    separate. Entries lacking a device_name are kept as-is. Order is preserved.
    """

    def _key(fs: dict[str, Any]) -> tuple[str, Any, Any]:
        return (os.path.realpath(fs["device_name"]), fs.get("size"), fs.get("used"))

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
    """Assemble per-GPU history records (utilization + memory).

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
            gpus.append(
                {"index": idx, "util_percent": _round(util, 1), **_gpu_mem_fields(st)}
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
                }
            )
    return gpus


def gpu_temps(
    gpu_stats: list[dict[str, Any]], gpu_data: list[dict[str, Any]]
) -> dict[str, float | None]:
    """{'GPU N': temperature|None} — nvidia-smi primary, Glances fallback.

    Emits the key even when temperature is None so the temps chart shows a gap
    for that GPU rather than dropping the series.
    """
    temps: dict[str, float | None] = {}
    if gpu_stats:
        for st in gpu_stats:
            idx = st.get("index")
            if idx is not None:
                temps[f"GPU {idx}"] = _round(st.get("temperature"), 1)
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


async def collect_history_sample(glances_url: str) -> dict[str, Any]:
    """Collect a single history sample from all data sources.

    Takes samples over the configured duration and computes min/avg/max
    for volatile metrics.
    """
    sample_duration: int = APP.state.history_sample_duration
    sample_count: int = APP.state.history_sample_count
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
    disk_to_mount: dict[str, str] = {}
    for fs in fs_data:
        device_name: str = fs.get("device_name", "")
        mnt_point: str = fs.get("mnt_point", "")
        if device_name and mnt_point:
            resolved: str = os.path.realpath(device_name)
            if resolved.startswith("/dev/"):
                disk_to_mount[resolved[5:]] = mnt_point
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


async def record_initial_sample(glances_url: str) -> None:
    """Record initial history sample on startup (runs in background)."""
    try:
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
    for rec in records:
        rec.pop("ip", None)
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
                "defaults": {
                    "record_interval": DEFAULT_HISTORY_RECORD_INTERVAL,
                    "sample_duration": DEFAULT_HISTORY_SAMPLE_DURATION,
                    "sample_count": DEFAULT_HISTORY_SAMPLE_COUNT,
                    "min_record_interval": DEFAULT_MANUAL_RECORD_MIN_INTERVAL,
                    "ping_enabled": True,
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

    ping_enabled = body.get("ping_enabled")
    if ping_enabled is not None and APP.state.ping_enabled != (ping_enabled is True):
        APP.state.ping_enabled = ping_enabled is True
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
            "History settings updated: interval=%ds, duration=%ds, count=%d, min_record=%ds, ping=%s",
            APP.state.history_record_interval,
            APP.state.history_sample_duration,
            APP.state.history_sample_count,
            APP.state.manual_record_min_interval,
            APP.state.ping_enabled,
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


def _get_gpu_info() -> list[dict[str, int | float | str | None]] | dict[str, str]:
    """Get detailed GPU info from nvidia-smi (sync, called from thread pool).

    Returns a list of GPU dicts on success, or an error dict on failure.
    """
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
    if result.returncode != 0:
        error_msg = result.stderr.strip() or f"Exit code {result.returncode}"
        return {"error": "nvidia-smi failed", "detail": error_msg}

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
    """Get detailed GPU info from nvidia-smi."""
    try:
        result = await asyncio.to_thread(_get_gpu_info)
    except FileNotFoundError:
        logger.debug("nvidia-smi not found")
        return Response(
            content=json.dumps(
                {
                    "error": "nvidia-smi not found",
                    "detail": "NVIDIA drivers may not be installed",
                }
            ),
            media_type="application/json",
        )
    except subprocess.TimeoutExpired:
        logger.warning("nvidia-smi timed out")
        return Response(
            content=json.dumps(
                {
                    "error": "nvidia-smi timeout",
                    "detail": "Command took longer than 5s",
                }
            ),
            media_type="application/json",
        )
    except Exception as e:
        logger.warning("nvidia-smi error: %s", e)
        return Response(
            content=json.dumps({"error": "GPU info unavailable", "detail": str(e)}),
            media_type="application/json",
        )

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
    """Check if a port is available for binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
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
