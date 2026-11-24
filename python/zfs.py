"""ZFS pool health for the server's own alarm source. Missing observations keep
previous alarms unresolved; unreadable expected pools raise an alarm.

Alarms (keys under ``zfs/``):
  zfs/unreadable            pools are expected but ``zpool`` is missing, fails, hangs
                            or prints what this cannot parse
  zfs/json-unsupported      zpool predates ``-j`` (OpenZFS < 2.3): health only
  zfs/<pool>/missing        an expected pool is not imported
  zfs/<pool>/state          the pool is not ONLINE
  zfs/<pool>/errors         read + write + checksum errors on any vdev, plus data errors
  zfs/<pool>/spare/<guid>/state  a spare is unavailable or unhealthy
  zfs/<pool>/never-scrubbed no scan on record
  zfs/<pool>/scrub-overdue  the last finished scrub ended more than ``scrub_max_days`` ago
Notes: a scrub or resilver in progress, a last scan that was a resilver or was
canceled, zpool installed with no pools.

Pools are *expected* from, strongest first: config ``zfs_pools``; pools this
server has seen before (``seen_file``, so a pool that vanishes alarms); disks
whose filesystem label says ``zfs_member`` (lsblk, unprivileged; the label is the
pool name). A box with none of these and no zpool gets no findings at all.

Read-only ``zpool status`` needs no root where /dev/zfs is 0666 (OpenZFS's udev
rule, shipped by Ubuntu's zfsutils-linux). It is run without ``-v`` (the error
log needs privilege), with LC_ALL=C, for every pool. A suspended pool can hang it
in uninterruptible sleep, where even a kill does not reap it: so it runs from a
daemon thread with a deadline, is killed and never waited for, and no second run
starts while it is still alive."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Final

from hostcheck import Findings

ZPOOL_TIMEOUT_SEC: Final[float] = 10.0
# Ubuntu's cron scrubs on the second Sunday of each month: gaps of 28 or 35 days,
# so 35 would alarm for hours in some months. Config: zfs_scrub_max_days.
SCRUB_MAX_DAYS: Final[float] = 38.0
VDEV_GROUPS: Final[tuple[str, ...]] = (
    "vdevs",
    "logs",
    "special",
    "dedup",
    "l2cache",
)

_lock: threading.Lock = threading.Lock()
_running: subprocess.Popen[str] | None = None


class Hung(Exception):
    """zpool did not answer in time (or an earlier run is still stuck)."""


def run_zpool(
    args: list[str], timeout: float = ZPOOL_TIMEOUT_SEC
) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) of zpool, or Hung. See the module docstring."""
    global _running
    with _lock:
        if _running is not None and _running.poll() is None:
            raise Hung(f"an earlier zpool (pid {_running.pid}) is still stuck")
        proc: subprocess.Popen[str] = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        _running = proc
    out: dict[str, str] = {}

    def read() -> None:
        stdout, stderr = proc.communicate()
        out.update(stdout=stdout, stderr=stderr)

    reader: threading.Thread = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()  # never waited for: a process in D state would block us too
        reader.join(1.0)  # an ordinary hang dies and is reaped here at once
        raise Hung(f"zpool did not answer within {timeout:g} s (a suspended pool?)")
    return proc.returncode, out.get("stdout", ""), out.get("stderr", "")


def zfs_member_labels(run: Any = subprocess.run) -> set[str]:
    """Pool names from disk labels (lsblk; no root needed). Empty on any failure."""
    try:
        result: subprocess.CompletedProcess[str] = run(
            ["lsblk", "-J", "-o", "NAME,FSTYPE,LABEL"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        devices: Any = json.loads(result.stdout).get("blockdevices", [])
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        return set()
    names: set[str] = set()

    def walk(nodes: Any) -> None:
        node: Any
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict):
                if node.get("fstype") == "zfs_member" and isinstance(
                    node.get("label"), str
                ):
                    names.add(node["label"])
                walk(node.get("children"))

    walk(devices)
    return names


def _read_seen(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    return {p for p in data if isinstance(p, str)} if isinstance(data, list) else set()


def _write_seen(path: Path | None, pools: set[str]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(exist_ok=True)
        tmp: Path = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(sorted(pools)))
        os.replace(tmp, path)
    except OSError:
        pass


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _counter(value: Any) -> int | None:
    """An available nonnegative integer; missing or malformed is not zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def vdev_errors(pool: dict[str, Any]) -> tuple[int, bool]:
    """(Known error sum, all active counters available).

    The top-level spares inventory includes idle devices without I/O counters.
    A spare in use is also in the active vdev tree, where its counters do count.
    """
    total: int = 0
    complete: bool = isinstance(pool.get("vdevs"), dict) and bool(pool["vdevs"])

    def walk(vdevs: Any) -> None:
        nonlocal total, complete
        if not isinstance(vdevs, dict):
            complete = False
            return
        vdev: Any
        for vdev in vdevs.values():
            if not isinstance(vdev, dict):
                complete = False
                continue
            key: str
            for key in ("read_errors", "write_errors", "checksum_errors"):
                value: int | None = _counter(vdev.get(key))
                if value is None:
                    complete = False
                else:
                    total += value
            if "vdevs" in vdev:
                walk(vdev["vdevs"])

    group: str
    for group in VDEV_GROUPS:
        if group in pool:
            walk(pool[group])
    return total, complete


def pool_findings(
    name: str, pool: dict[str, Any], now: float, scrub_max_days: float
) -> Findings:
    """The alarms and notes of one parsed pool."""
    found: Findings = Findings(checked=[f"zfs/{name}/state"])
    state: str = str(pool.get("state", "UNKNOWN"))
    if state != "ONLINE":
        found.alarms.append(
            {
                "key": f"zfs/{name}/state",
                "text": f"zfs {name}: pool is {state}",
                "value": state,
            }
        )
    errors, errors_complete = vdev_errors(pool)
    data: int | None = _counter(pool.get("error_count"))
    if errors_complete and data is not None:
        found.checked.append(f"zfs/{name}/errors")
    else:
        found.notes.append(f"zfs {name}: error counters incomplete")
    if errors or data:
        found.alarms.append(
            {
                "key": f"zfs/{name}/errors",
                "text": f"zfs {name}: {errors} device errors, {data if data is not None else 'unknown'} data errors",
                "value": errors + (data or 0),
            }
        )
    # Idle spares have state but no I/O counters. Check them separately, using
    # their GUID so renamed paths cannot inherit or resolve another disk's alarm.
    spares: Any = pool.get("spares", {})
    if not isinstance(spares, dict):
        found.notes.append(f"zfs {name}: spare inventory unreadable")
        spares = {}
    for label, spare in spares.items():
        if not isinstance(spare, dict):
            found.notes.append(f"zfs {name}: spare {label} unreadable")
            continue
        guid: int | None = _counter(spare.get("guid"))
        spare_state: Any = spare.get("state")
        if not guid:
            found.notes.append(f"zfs {name}: spare {label} identity unreadable")
            continue
        key: str = f"zfs/{name}/spare/{guid}/state"
        if spare_state in ("AVAIL", "INUSE"):
            found.checked.append(key)
        elif spare_state in ("DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"):
            found.checked.append(key)
            found.alarms.append(
                {
                    "key": key,
                    "text": f"zfs {name}: spare {label} is {spare_state}",
                    "value": spare_state,
                }
            )
        else:
            found.notes.append(f"zfs {name}: spare {label} state unreadable")
    scan: Any = pool.get("scan_stats")
    if scan is None:
        found.checked.append(f"zfs/{name}/never-scrubbed")
        found.alarms.append(
            {"key": f"zfs/{name}/never-scrubbed", "text": f"zfs {name}: never scrubbed"}
        )
        return found
    if not isinstance(scan, dict):
        found.notes.append(f"zfs {name}: scan state unreadable; scrub age unknown")
        return found
    function: str = str(scan.get("function", ""))
    scan_state: str = str(scan.get("state", ""))
    if scan_state in ("SCANNING", "ERRORSCRUBBING"):
        todo: int = _int(scan.get("to_examine"))
        pct: str = f" {100 * _int(scan.get('issued')) / todo:.0f}%" if todo else ""
        paused: str = ", paused" if _int(scan.get("scrub_pause")) else ""
        found.notes.append(f"zfs {name}: {function.lower()} in progress{pct}{paused}")
    elif function == "SCRUB" and scan_state == "FINISHED":
        ended: int | None = _counter(scan.get("end_time"))
        if ended is None or not 0 < ended <= now:
            found.notes.append(f"zfs {name}: last scrub completion time unknown")
            return found
        found.checked.extend(
            [f"zfs/{name}/never-scrubbed", f"zfs/{name}/scrub-overdue"]
        )
        days: float = (now - ended) / 86400
        if days > scrub_max_days:
            found.alarms.append(
                {
                    "key": f"zfs/{name}/scrub-overdue",
                    "text": f"zfs {name}: last scrub {days:.0f} days ago"
                    f" (limit {scrub_max_days:g})",
                }
            )
    else:
        found.notes.append(
            f"zfs {name}: last scan was a {function.lower() or 'scan'}"
            f" ({scan_state.lower() or 'unknown'}); scrub age unknown"
        )
    return found


def zfs_findings(
    expected_config: Any = None,
    scrub_max_days: Any = None,
    seen_file: Path | None = None,
    now: float | None = None,
    run: Any = run_zpool,
    labels: Any = zfs_member_labels,
    zpool: str | None = None,
    module_loaded: bool | None = None,
) -> tuple[Findings, dict[str, Any]]:
    """(findings, a summary for the API: {status, error?, pools}). See the module
    docstring for what counts as expected and what alarms."""
    now = time.time() if now is None else now
    max_days: float = (
        float(scrub_max_days)
        if isinstance(scrub_max_days, (int, float))
        and not isinstance(scrub_max_days, bool)
        else SCRUB_MAX_DAYS
    )
    configured: set[str] = (
        {p for p in expected_config if isinstance(p, str)}
        if isinstance(expected_config, list)
        else set()
    )
    seen: set[str] = _read_seen(seen_file)
    zpool = zpool if zpool is not None else shutil.which("zpool")
    module_loaded = (
        Path("/sys/module/zfs").exists() if module_loaded is None else module_loaded
    )
    expected: set[str] = configured | seen
    if zpool is None and not expected and not module_loaded:
        expected |= labels()
        if not expected:
            return Findings(), {"status": "absent", "pools": []}
    else:
        expected |= labels()
    found: Findings = Findings(checked=["zfs/unreadable", "zfs/json-unsupported"])

    def unreadable(why: str) -> tuple[Findings, dict[str, Any]]:
        if expected:
            found.alarms.append(
                {
                    "key": "zfs/unreadable",
                    "text": f"zfs: cannot read pool state ({why});"
                    f" expected pools: {', '.join(sorted(expected))}",
                }
            )
        else:
            found.notes.append(f"zfs: cannot read pool state ({why})")
        # The pools' own alarms are held, not cleared: nothing was looked at.
        return found, {"status": "error", "error": why, "pools": []}

    if zpool is None:
        return unreadable("zpool is not installed")
    try:
        rc, stdout, stderr = run([zpool, "status", "-j", "--json-int"])
    except Hung as e:
        return unreadable(str(e))
    except OSError as e:
        return unreadable(f"cannot run zpool: {e}")
    why: str = (stderr.strip() or stdout.strip() or f"exit {rc}").splitlines()[0]
    if rc != 0 and ("invalid option" in stderr or "unrecognized option" in stderr):
        found, summary = _health_only(zpool, run, found, why)
        if summary["status"] == "ok":
            _remember_pools(found, summary["pools"], expected, seen, seen_file)
        return found, summary
    if rc != 0:
        return unreadable(why)
    try:
        pools: Any = json.loads(stdout).get("pools")
        if not isinstance(pools, dict):
            raise ValueError("no pools object")
        if any(
            not name
            or not isinstance(pool, dict)
            or not isinstance(pool.get("state"), str)
            or not pool["state"]
            for name, pool in pools.items()
        ):
            raise ValueError("invalid pool entry")
    except (ValueError, AttributeError) as e:
        return unreadable(f"unparseable zpool output: {e}")
    summary: list[dict[str, Any]] = []
    name: str
    for name, pool in pools.items():
        found = found.merge(pool_findings(name, pool, now, max_days))
        scan: Any = pool.get("scan_stats")
        errors, errors_complete = vdev_errors(pool)
        summary.append(
            {
                "name": name,
                "state": pool.get("state"),
                "errors": errors if errors_complete else None,
                "data_errors": _counter(pool.get("error_count")),
                "scan": {
                    k: scan.get(k)
                    for k in ("function", "state", "start_time", "end_time")
                }
                if isinstance(scan, dict)
                else None,
            }
        )
    _remember_pools(found, summary, expected, seen, seen_file)
    return found, {"status": "ok", "pools": summary}


def _remember_pools(
    found: Findings,
    pools: list[dict[str, Any]],
    expected: set[str],
    seen: set[str],
    seen_file: Path | None,
) -> None:
    """Shared bookkeeping after either JSON or legacy output was read."""
    names: set[str] = {pool["name"] for pool in pools}
    _missing(expected, names, found)
    if not names and not expected:
        found.notes.append("zfs: installed, no pools imported")
    if names - seen:
        _write_seen(seen_file, seen | names)


def _missing(expected: set[str], present: set[str], found: Findings) -> None:
    name: str
    for name in sorted(expected):
        found.checked.append(f"zfs/{name}/missing")
        if name not in present:
            found.alarms.append(
                {
                    "key": f"zfs/{name}/missing",
                    "text": f"zfs {name}: pool is not imported",
                }
            )


def _health_only(
    zpool: str, run: Any, found: Findings, why: str
) -> tuple[Findings, dict[str, Any]]:
    """OpenZFS before 2.3 has no ``-j``: pool health from ``zpool list``, and an
    alarm that the errors and scrub age are not being watched."""
    found.alarms.append(
        {
            "key": "zfs/json-unsupported",
            "text": f"zfs: zpool has no JSON output ({why}): only pool health is watched,"
            " not errors or scrubs",
        }
    )
    try:
        rc, stdout, stderr = run([zpool, "list", "-H", "-p", "-o", "name,health"])
    except (Hung, OSError) as e:
        rc, stdout, stderr = 1, "", str(e)
    if rc != 0:
        found.alarms.append({"key": "zfs/unreadable", "text": f"zfs: {stderr.strip()}"})
        return found, {"status": "error", "error": stderr.strip(), "pools": []}
    rows: list[list[str]] = [
        [str(part) for part in line.split("\t")] for line in stdout.splitlines()
    ]
    if any(len(row) != 2 or not all(row) for row in rows):
        why = "unparseable zpool list output"
        found.alarms.append({"key": "zfs/unreadable", "text": f"zfs: {why}"})
        return found, {"status": "error", "error": why, "pools": []}
    name: str
    health: str
    for name, health in rows:
        found.checked.append(f"zfs/{name}/state")
        if health != "ONLINE":
            found.alarms.append(
                {
                    "key": f"zfs/{name}/state",
                    "text": f"zfs {name}: pool is {health}",
                }
            )
    return found, {
        "status": "ok",
        "pools": [{"name": name, "state": health} for name, health in rows],
    }
