#!/bin/bash
# health-collect.sh -- root-only snapshot of hardware health for the dashboard
# (and any other reader, e.g. a login MOTD script). THE ONE PLACE that decides
# "is something wrong": it emits `alarms` (and `info`); readers just print them.
#
# Writes /var/lib/horatio/state.json (0644) so unprivileged readers --
# the dashboard runs as an ordinary user -- never need sudo. Written to .tmp then renamed,
# so a reader can never see a partial file.
#
# Collects what needs root:
#   - IPMI SEL: memory events (ECC), and hardware faults (PSU/fan/drive/thermal)
#     whose description matches the fault phrases in the config file. No state is
#     kept: an event counts as "recent" when the BMC's timestamp is within 7 days.
#   - smartctl --all --json per drive WITHOUT waking a drive in standby, including
#     'Elements in grown defect list', which smartd's attrlog does NOT record
#     and which is the only useful early warning on the SAS drives
#   (fans, temperatures and power are health-sensors.sh's job, every minute,
#   in sensors.json; this run alarms if that file goes stale)
#
# Also snapshots what needs no privileges but belongs in the same file: EDAC
# per-DIMM counters, md array state from sysfs, the backup timer's last trigger,
# and `systemctl --failed` (the catch-all for anything that is not a drive, a
# DIMM or the backup: a service that died gets one alarm line instead of silence).
#
# Every collection command's stderr AND a non-zero exit code go to a sidecar
# .err file; the python step turns any non-empty one into a `collector:` alarm,
# so a broken or wedged tool shows up on the dashboard instead of silently
# producing an empty section that reads as healthy.
#
# Usage: sudo systemctl start horatio-collect.service
# (the timer runs it every 15 minutes; starting the unit by hand serialises on
# it, running this script directly does not).

set -uo pipefail
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin

OUT="${1:-/var/lib/horatio/state.json}"

[ "$(id --user)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# mkdir, not `install --directory`: the latter chmods an EXISTING directory too
# (running with OUT=/tmp/x.json would strip the sticky bit from /tmp as root).
mkdir --parents --mode 0755 "$(dirname "$OUT")"
tmp=$(mktemp --directory) || exit 1     # no set -e here; an empty $tmp would redirect into /
trap 'rm -rf "$tmp"' EXIT

# --- captures ----------------------------------------------------------------
# `timeout` on everything that talks to the BMC or a drive: a wedged BMC or a
# drive that stops answering must not hang the collector forever. The SEL gets
# longer because in-band IPMI fetches one record per round trip.
# `|| echo "exit $?"` records a non-zero exit in the sidecar: `timeout` kills
# silently (124), so without it a wedged BMC would look like an empty SEL.
# No BMC (a laptop, a desktop): skip the IPMI capture instead of alarming on a
# missing tool; the python notes it once as `info`. `command -v` is a presence
# test, its stdout is the path, not an error.
if command -v ipmitool > /dev/null; then
    timeout 180 ipmitool sel list          > "$tmp/sel.txt"  2> "$tmp/sel.err"  || echo "exit $?" >> "$tmp/sel.err"
else
    : > "$tmp/sel.txt"; : > "$tmp/sel.err"; : > "$tmp/no-ipmitool"
fi
# Fans, temperatures and power are NOT here: health-sensors.sh snapshots them
# every minute into sensors.json (this run checks that file is fresh, below).
# What each drive holds (partitions, md members, LVM) for the dashboard's drive
# card. The TREE output (no --list) repeats a multi-parent device such as md0
# under each of its members; --list prints it only once.
timeout 60  lsblk --json --output NAME,TYPE,MOUNTPOINTS > "$tmp/lsblk.json" 2> "$tmp/lsblk.err" || echo "exit $?" >> "$tmp/lsblk.err"
# --plain drops the bullet column, --no-legend the header/footer: one unit per line.
timeout 60  systemctl --failed --plain --no-legend > "$tmp/failed.txt" 2> "$tmp/failed.err" || echo "exit $?" >> "$tmp/failed.err"
# error-and-worse from every unit and the kernel in the last day.
timeout 60  journalctl --priority err --since -24hours --quiet --no-pager --output short-iso \
                                             > "$tmp/journal-err.txt" 2> "$tmp/journal-err.err" || echo "exit $?" >> "$tmp/journal-err.err"
# Ask the TIMER, never `systemctl show system-backup.service`: a oneshot that is
# not running is unloaded and `show` prints defaults for it. With
# --timestamp=unix (systemd >= 247) LastTriggerUSec is "@<epoch>", or empty when
# the timer never fired; the "@" is stripped here. No date(1) round trip: a
# locale or format it could not parse would have read as "never fired".
# The timer's name is the config's `backup_timer` (absent or null = this box has
# no backup timer: no section, no alarm). Chosen here, once,
# and handed to the python via backup-timer.name. A config that does not parse
# lands in the sidecar (and crashes the python stage on the same file, on purpose).
: > "$tmp/backup-timer.txt"; : > "$tmp/backup-timer.err"
BACKUP_TIMER=$(python3 -c '
import json, sys
try:
    config = json.load(open(sys.argv[1]))
except FileNotFoundError:
    config = {}
print(config.get("backup_timer") or "")
' "${HEALTH_CONFIG:-/etc/horatio/config.json}" 2>> "$tmp/backup-timer.err") \
    || echo "exit $?" >> "$tmp/backup-timer.err"
echo "$BACKUP_TIMER" > "$tmp/backup-timer.name"
if [ -n "$BACKUP_TIMER" ]; then
    last=$(timeout 60 systemctl show "$BACKUP_TIMER" --property LastTriggerUSec --value --timestamp=unix 2>> "$tmp/backup-timer.err") \
        || echo "exit $?" >> "$tmp/backup-timer.err"
    echo "${last#@}" > "$tmp/backup-timer.txt"
fi

# smartctl's exit status is a BITMASK, so the python step decodes it:
#   bits 0-1 (1, 2)     could not read the drive: bad command line, open or
#                       identify failed
#   exit 3              `--nocheck=standby,3`: the drive was in standby and was LEFT
#                       ASLEEP (3 is the status we ask for; it happens to be
#                       bits 0+1, so it is checked before the bits are)
#   bit 2 (4)           a SMART command failed; if it was an optional one the
#                       JSON is still complete, if a mandatory one there is no
#                       smart_status in the JSON and the drive counts as unreadable
#   bit 3 (8)           SMART health check says the drive is FAILING
#   bits 4-6 (16..64)   attributes at/under threshold now or in the past,
#                       entries in the error log
#   bit 7 (128)         the self-test log has errors: a scheduled test failed
# `timeout` exits 124 with no JSON at all, which the missing smart_status catches.
# SATA/SAS/USB disks and NVMe namespaces; smartctl speaks both. No smartmontools
# at all is an ALARM (the marker file below), not a quiet gap: every box has drives.
if command -v smartctl > /dev/null; then
    for dev in /dev/sd? /dev/nvme?n?; do
        [ -b "$dev" ] || continue     # no match leaves the literal glob
        name=$(basename "$dev")
        timeout 60 smartctl --nocheck=standby,3 --all --json "$dev" > "$tmp/smart_$name.json" 2> "$tmp/smart_$name.err"
        echo $? > "$tmp/smart_$name.rc"
    done
else
    : > "$tmp/no-smartctl"
fi

python3 - "$tmp" "$OUT" <<'PY'
import glob, json, os, re, sys, time

tmp, out = sys.argv[1], sys.argv[2]

# ATA attributes whose raw value is a real media-error count. 199 (UDMA CRC)
# is recorded but never alarmed (cable noise, and some vendors pack other
# counters into it); 1 and 7 are Seagate packed operation counters and are never read.
ATA_WATCH = {5: "reallocated", 187: "reported_uncorrect", 188: "command_timeout",
             197: "pending", 198: "offline_uncorrectable", 199: "udma_crc"}
ATA_ALARM = {"reallocated", "reported_uncorrect", "command_timeout", "pending",
             "offline_uncorrectable"}
MD_BAD_STATES = {"faulty", "blocked", "write_error", "want_replacement"}
CE_WARN = 50            # correctable ECC errors since boot before we say so
BACKUP_MAX_AGE_H = 30   # nightly timer; 30 h allows one late/skipped fire
SEL_RECENT_DAYS = 7

alarms, info = [], []

def read(name):
    # Every file read here was written by a redirection in the bash stage above,
    # so a missing one is a bug in this script and should crash into the journal.
    with open(os.path.join(tmp, name)) as f:
        return f.read()

def sysread(*parts):
    """One stripped line from a sysfs file, or None if it does not exist."""
    try:
        with open(os.path.join(*parts)) as f:
            return f.read().strip()
    except OSError:
        return None

state = {"ts": int(time.time())}

# ---- config (per-machine SEL fault policy) -----------------------------------
# The SEL logs real faults and routine chatter under the same sensor types, and
# which events count as faults is deployment-specific (a lost PSU is a fault on
# a dual-PSU box, normal on a single-PSU one; chassis intrusion is usually
# noise). So the fault phrases live in a config file, editable without touching
# this script. Matched case-insensitively as substrings of the event
# description. A missing file uses the built-in default; malformed JSON crashes
# the snapshot on purpose (loud -> stale), like every other bad input here.
SEL_FAULT_PATTERNS_DEFAULT = [
    "AC lost", "Redundancy Lost", "Lower Critical going", "Upper Critical going",
    "Drive Fault", "failure", "failed",
]
try:
    with open(os.environ.get("HEALTH_CONFIG", "/etc/horatio/config.json")) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}
sel_fault_patterns = [p.lower() for p in config.get("sel_fault_patterns", SEL_FAULT_PATTERNS_DEFAULT)]
# SAS grown-defect counts keyed by serial (device letters move between boots).
# A serial that is not listed is watched from 0, so a replacement drive is never
# silently skipped; add it to the config once its factory count is known.
grown_defect_baseline = config.get("grown_defect_baseline", {})
if os.path.exists(os.path.join(tmp, "no-smartctl")):
    alarms.append("smartmontools not installed: drives are not monitored")
if os.path.exists(os.path.join(tmp, "no-ipmitool")):
    info.append("ipmitool not installed: no BMC event log")

# ---- SEL, memory events ------------------------------------------------------
# Dell SEL lines look like (this script runs under LC_ALL=C, where ipmitool's
# %x date is a 2-digit year; other locales print 4 digits, so both are accepted)
#   "  1a | 01/02/24 | 03:04:05 UTC | Memory #0x02 | Uncorrectable ECC (UnCorrectable ECC |  DIMMA1) | Asserted"
#   "  1b | 01/03/24 | 06:07:08 UTC | Memory #0x1b | Transition to Critical from less severe | Asserted"
# "Recent" is judged from the BMC's own timestamp, so nothing is carried between
# runs. The SEL is the only record of events that happen during POST: save it
# (`ipmitool sel save`) before clearing.
#
# " <hex id> | MM/DD/YY | ... ". ipmitool formats the date with strftime %x: a
# 2-digit year under LC_ALL=C (this script exports it), 4-digit in other locales.
# Only the DATE is parsed: the time column varies (24 h vs "07:45:47 PM", zone or
# not) and day granularity is enough for a 7-day window (an event at 23:59 counts
# from 00:00, so the window is really 7-8 days).
SEL_DATE = re.compile(r"^\s*\w+\s*\|\s*(\d\d/\d\d/\d{2,4})\s*\|")

def sel_when(line):
    """Epoch of the line's date, or None when it has none: " 1b | Pre-Init |0000000123| ..."
    from a BMC whose clock was unset, or an undated "OEM record e0" entry."""
    m = SEL_DATE.match(line)
    if not m:
        return None
    d = m.group(1)
    return time.mktime(time.strptime(d, "%m/%d/%y" if len(d) == 8 else "%m/%d/%Y"))

def sel_recent(line):
    # Undated lines are never "recent": the alternative is an alarm only `sel clear` ends.
    when = sel_when(line)
    return when is not None and state["ts"] - when < SEL_RECENT_DAYS * 86400

sel_lines = read("sel.txt").splitlines()
mem = [l.strip() for l in sel_lines if "mem" in l.lower() or "ecc" in l.lower()]
recent = [l for l in mem if sel_recent(l)]
state["sel_memory"] = {"count": len(mem), "recent": recent}
if recent:
    alarms.append(f"SEL: {len(recent)} memory events in {SEL_RECENT_DAYS}d ({len(mem)} total), latest: {recent[-1]}")

# ---- SEL, hardware faults (PSU / fan / drive / thermal) ----------------------
# The non-memory faults from the same log. A real 6-column row whose description
# (col 5) matches a configured fault phrase and whose direction (col 6) is
# "Asserted" -- the onset, not a "Deasserted" recovery. Everything else (chassis
# intrusion, presence detected, redundancy REGAINED, non-critical warnings) just
# does not match. Memory lines are the mem/ecc slice above and are skipped here;
# they also carry an embedded "|" in the description, so their column count and
# direction position differ -- another reason not to run them through this.
def sel_hardware_fault(line):
    parts = line.split("|")
    if len(parts) < 6 or "mem" in line.lower() or "ecc" in line.lower():
        return False
    # The direction is always the LAST column; a description with its own "|"
    # shifts the columns, so index from the end and rejoin the middle.
    if parts[-1].strip().lower() != "asserted":
        return False
    desc = "|".join(parts[4:-1]).strip().lower()
    return any(p in desc for p in sel_fault_patterns)

hw = [l.strip() for l in sel_lines if sel_hardware_fault(l)]
hw_recent = [l for l in hw if sel_recent(l)]
state["sel_hardware"] = {"count": len(hw), "recent": hw_recent}
if hw_recent:
    alarms.append(f"SEL: {len(hw_recent)} hardware events in {SEL_RECENT_DAYS}d ({len(hw)} total), latest: {hw_recent[-1]}")

# A dated-looking line whose date did not parse is a format change (locale, ipmitool
# version), not a hardware event: say so instead of silently under-counting.
unparsed = [l.strip() for l in sel_lines
            if "|" in l and "Pre-Init" not in l and "OEM record" not in l and sel_when(l) is None]
if unparsed:
    alarms.append(f"collector: {len(unparsed)} SEL lines with unrecognised date format, e.g. {unparsed[-1]}")

# ---- EDAC per-DIMM (no privileges needed; counters RESET EVERY BOOT) ---------
# HEALTH_EDAC / HEALTH_UPTIME point the fixture run at a fake tree (README).
dimms = []
for d in sorted(glob.glob(os.path.join(os.environ.get("HEALTH_EDAC", "/sys/devices/system/edac/mc"), "mc*/dimm*"))):
    label = sysread(d, "dimm_label")
    if label is None:
        continue
    dimms.append({
        "path": "/".join(d.split("/")[-2:]),
        "label": label,          # generic sb_edac name, NOT Dell A1..B12
        "ce_count": int(sysread(d, "dimm_ce_count") or 0),
        "ue_count": int(sysread(d, "dimm_ue_count") or 0),
    })
uptime = int(float(open(os.environ.get("HEALTH_UPTIME", "/proc/uptime")).read().split()[0]))
ce_total = sum(d["ce_count"] for d in dimms)
ue_total = sum(d["ue_count"] for d in dimms)
state["edac"] = {
    "uptime_seconds": uptime,
    "dimms": dimms,
    "ce_total": ce_total,
    "ue_total": ue_total,
}
if ue_total > 0:
    alarms.append(f"ECC: {ue_total} uncorrectable errors since boot")
elif ce_total >= CE_WARN:
    alarms.append(f"ECC: {ce_total} correctable errors since boot")

# ---- drives ------------------------------------------------------------------
drives = []
for path in sorted(glob.glob(os.path.join(tmp, "smart_*.rc"))):     # sdX and nvmeXnY
    name = os.path.basename(path)[6:-3]      # smart_<name>.rc
    rc = int(read(f"smart_{name}.rc"))
    if rc == 3:                              # standby: left asleep, nothing read
        drives.append({"device": name, "standby": True})
        info.append(f"{name}: standby")
        continue

    try:
        with open(os.path.join(tmp, f"smart_{name}.json")) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    # Bits 0-1 (bad args, open/identify failed) mean nothing was read. No `smart_status`
    # in the JSON means smartctl died before printing it (`timeout`, exit 124) or SMART
    # is disabled/unsupported (bit 2 on a mandatory command). Bit 2 alone, with the
    # JSON present, is an optional command that failed and the rest is complete.
    if rc & 3 or "smart_status" not in s:
        msgs = [m.get("string") for m in (s.get("smartctl") or {}).get("messages") or []]
        why = read(f"smart_{name}.err").strip() or "; ".join(m for m in msgs if m) \
              or f"smartctl exit {rc}, no JSON"
        drives.append({"device": name, "error": why})
        alarms.append(f"{name}: unreadable ({why})")
        continue

    serial = s.get("serial_number")
    entry = {
        "device": name,
        "model": s.get("model_name"),
        "serial": serial,
        "protocol": (s.get("device") or {}).get("protocol"),
        "passed": (s.get("smart_status") or {}).get("passed"),
        "temperature_c": (s.get("temperature") or {}).get("current"),
        "power_on_hours": (s.get("power_on_time") or {}).get("hours"),
        "smartctl_rc": rc,       # bits 4-7 are otherwise invisible
        # rotation_rate is 0 for solid state, the rpm for spinning (ATA and SCSI alike).
        "kind": "SSD" if s.get("rotation_rate") == 0 else "HDD" if s.get("rotation_rate") else None,
        "size_bytes": (s.get("user_capacity") or {}).get("bytes"),
    }
    if entry["passed"] is False:
        alarms.append(f"{name}: SMART health FAILED")
    # NVMe has no attribute table; its health log is the whole story. `passed`
    # already covers critical_warning (spare below threshold, over temperature,
    # read-only, ...); media errors and wear-out are separate counters.
    nvme = s.get("nvme_smart_health_information_log") or {}
    if nvme:
        entry["media_errors"] = nvme.get("media_errors")
        entry["percentage_used"] = nvme.get("percentage_used")
        if (nvme.get("media_errors") or 0) > 0:
            alarms.append(f"{name}: media_errors={nvme['media_errors']}")
        if (nvme.get("percentage_used") or 0) >= 100:
            alarms.append(f"{name}: percentage_used={nvme['percentage_used']} (endurance spent)")
    if rc & 128:    # bit 7: the self-test log has errors. ATA drops entries outdated by
                    # a later successful extended test; SCSI alarms while any failed
                    # entry is in its 20-entry log.
        alarms.append(f"{name}: self-test log has errors")
    # smartd's `-n standby` skips scheduled tests on a sleeping drive, so one that
    # is never awake at test time is never tested. Info, not alarm: nothing is
    # wrong with the drive, it is just unexercised. ATA keeps a count in the
    # self-test log; SCSI has a scsi_self_test_0 entry once any test has run.
    if "ata_smart_self_test_log" in s:
        tests = (s["ata_smart_self_test_log"].get("standard") or {}).get("count", 0)
    elif "scsi_grown_defect_list" in s:
        tests = 1 if "scsi_self_test_0" in s else 0
    else:
        tests = None
    if tests == 0:
        info.append(f"{name}: no self-test on record")

    # Drives that report a trip point (SAS drives, typically 68 C) get a 5 C margin.
    trip = (s.get("temperature") or {}).get("drive_trip")
    if trip is not None:
        entry["drive_trip_c"] = trip
        if entry["temperature_c"] is not None and entry["temperature_c"] >= trip - 5:
            alarms.append(f"{name}: {entry['temperature_c']}C, trip point {trip}C")

    # SAS: the grown defect list is the signal that matters. A SAS drive can
    # fail with every error counter at zero to the end; only this number and
    # the health bit move.
    if "scsi_grown_defect_list" in s:
        grown = s["scsi_grown_defect_list"]
        entry["grown_defect_list"] = grown
        base = grown_defect_baseline.get(serial, 0)
        entry["grown_defect_baseline"] = base
        if grown > base:
            alarms.append(f"{name} ({serial}): grown defects {grown} > baseline {base}")

    # SATA: the attributes that actually indicate media failure.
    got = {}
    for a in (s.get("ata_smart_attributes") or {}).get("table") or []:
        if a.get("id") in ATA_WATCH:
            got[ATA_WATCH[a["id"]]] = (a.get("raw") or {}).get("value")
    if got:
        entry["attrs"] = got
        bad = [f"{k}={v}" for k, v in got.items() if k in ATA_ALARM and (v or 0) > 0]
        if bad:
            alarms.append(f"{name}: {', '.join(bad)}")

    drives.append(entry)

# ---- what each drive holds (lsblk, no privileges) ---------------------------
# Physical drives are not mounts: md0 is two drives, / is an LVM volume on a
# partition. The lsblk tree repeats md0 under each member, so recursing from a
# disk down through its children collects every mountpoint it contributes to.
# An lsblk failure leaves the file empty; the .err sidecar alarms for it.
blockdevs = json.loads(read("lsblk.json") or '{"blockdevices": []}')["blockdevices"]

def mounts_under(dev, via):
    """Every mountpoint below a device, prefixed by the md / LVM device it goes through."""
    # A partition mounted directly needs no "via"; md / LVM / crypt show the path.
    here = via if dev["type"] in ("disk", "part") else f"{dev['name']} -> "
    out = {f"{here}{mp}" for mp in dev.get("mountpoints") or [] if mp}
    for child in dev.get("children") or []:
        out |= mounts_under(child, here)
    return out

holds = {b["name"]: mounts_under(b, "") for b in blockdevs}
for d in drives:
    d["holds"] = sorted(holds.get(d["device"], ()))

state["drives"] = drives

# ---- md arrays (sysfs, no privileges) ----------------------------------------
# HEALTH_SYSBLOCK exists only so a fake tree can be pointed at for testing.
sysblock = os.environ.get("HEALTH_SYSBLOCK", "/sys/block")
mds = []
for md in sorted(glob.glob(os.path.join(sysblock, "md*", "md"))):
    name = os.path.basename(os.path.dirname(md))
    members = []
    for dev in sorted(glob.glob(os.path.join(md, "dev-*"))):
        member = os.path.basename(dev)[4:]                     # dev-<name>
        # sysfs prints e.g. "in_sync,writemostly"; keep it as a list.
        flags = [f for f in (sysread(dev, "state") or "").split(",") if f]
        merrs = int(sysread(dev, "errors") or 0)
        members.append({"name": member, "state": flags, "errors": merrs})
        for f in flags:
            if f in MD_BAD_STATES:
                alarms.append(f"{name}: {member} state {f}")
        if merrs:
            alarms.append(f"{name}: {member} errors={merrs}")
    degraded = int(sysread(md, "degraded") or 0)
    sync_action = sysread(md, "sync_action")
    array_state = sysread(md, "array_state")
    # Blocks that differed between mirrors at the last `check`; persists until
    # the next check. On RAID1 a small count can be benign (in-flight writes).
    # It climbs while a check/resync/repair RUNS, so only judge it once idle.
    mismatch = int(sysread(md, "mismatch_cnt") or 0)
    mds.append({"name": name, "level": sysread(md, "level"), "degraded": degraded,
                "sync_action": sync_action, "array_state": array_state,
                "mismatch_cnt": mismatch, "members": members})
    if mismatch and sync_action == "idle":
        alarms.append(f"{name}: mismatch_cnt={mismatch}")
    if degraded:
        alarms.append(f"{name}: DEGRADED ({degraded} missing)")
    if array_state in ("inactive", "readonly"):
        alarms.append(f"{name}: array_state {array_state}")
    if sync_action in ("check", "resync", "repair", "recover", "reshape"):
        info.append(f"{name}: {sync_action} in progress")
state["md"] = mds

# ---- backup ------------------------------------------------------------------
# Only "did the timer fire recently". A run that FAILS stays in systemd's failed
# state until the next start, so it already shows up as `unit failed:` below,
# and ExecStopPost= in the unit mails the log.
backup_timer = read("backup-timer.name").strip()     # chosen by the bash stage from the config
if backup_timer:
    last = read("backup-timer.txt").strip()      # epoch seconds, or empty: never fired
    last_trigger = int(last) if last else None
    age_h = round((state["ts"] - last_trigger) / 3600, 1) if last_trigger else None
    state["backup"] = {"timer": backup_timer, "last_trigger": last_trigger, "age_hours": age_h}
    if age_h is None:
        alarms.append(f"backup: {backup_timer} has never fired")
    elif age_h > BACKUP_MAX_AGE_H:
        alarms.append(f"backup: last trigger {int(age_h)}h ago")
else:
    state["backup"] = None       # config says this box has no backup timer

# ---- the 60 s sensors snapshot (health-sensors.sh) ---------------------------
# Its unit sends no failure mail (one per minute would be noise), so this run is
# what notices a dead sensors timer and carries it to the dashboard and the mail.
sensors_path = os.path.join(os.path.dirname(out), "sensors.json")
try:
    sensors_age = int(time.time() - os.stat(sensors_path).st_mtime)
except FileNotFoundError:
    sensors_age = None
if sensors_age is None:
    alarms.append("collector: no sensors snapshot (horatio-sensors.timer never ran?)")
elif sensors_age > 300:
    alarms.append(f"collector: sensors snapshot {sensors_age}s old (horatio-sensors.timer stuck?)")

# ---- failed systemd units ----------------------------------------------------
# Each line is "UNIT LOAD ACTIVE SUB DESCRIPTION"; only the unit name is kept.
state["failed_units"] = [l.split()[0] for l in read("failed.txt").splitlines() if l.strip()]
for unit in state["failed_units"]:
    alarms.append(f"unit failed: {unit}")

# ---- journal errors (last 24 h) ----------------------------------------------
# Known-benign noise: ACPI IPMI region errors at every boot (a firmware quirk
# of e.g. Dell R730 boards), an empty kernel line, and sudo without a password (typos, and
# non-interactive sessions probing with sudo: "a password is required"). A
# constant, not the config file: that file is a hardware-fault allowlist, this
# is a log-noise denylist. Extend it from `journalctl --priority err --since
# -7days` on the box.
JOURNAL_IGNORE = re.compile(r"ACPI Error|kernel:\s*$|pam_unix\(sudo:auth\)|a password is required")
# short-iso: one line per message starting with its ISO timestamp, continuation
# lines indented. The "timestamp host unit[pid]:" prefix is three tokens; sudo
# pads its message with spaces after that, hence the strip.
JOURNAL_PREFIX = re.compile(r"^\S+ \S+ \S+ ")
msgs = [JOURNAL_PREFIX.sub("", l).strip() for l in read("journal-err.txt").splitlines()
        if re.match(r"\d{4}-\d\d-\d\dT", l) and not JOURNAL_IGNORE.search(l)]
distinct = set(msgs)
state["journal_errors"] = {"hours": 24, "count": len(msgs), "distinct": sorted(distinct)}
if msgs:
    # One transient error keeps this line for 24 h: intended, errors are surfaced.
    alarms.append(f"journal: {len(msgs)} error-level messages ({len(distinct)} distinct) in 24h, latest: {msgs[-1]}")

# ---- collection failures (any non-empty .err sidecar) ------------------------
# A tool that failed or timed out means this snapshot is partial, which must not
# read as healthy: it is an alarm, not a footnote. One exception: ipmitool says
# "SEL has no entries" on stderr (exit 0) when the log is empty, which is a fact,
# not a failure. The smart_*.err files belong to the drives stage above: an
# unreadable drive is already the rich `sdX: unreadable (...)` alarm, and stderr
# chatter from a drive that answered is deliberately not one.
for path in sorted(glob.glob(os.path.join(tmp, "*.err"))):
    if os.path.basename(path).startswith("smart_"):
        continue
    text = read(os.path.basename(path)).strip()
    if text and text != "SEL has no entries":
        alarms.append(f"collector: {os.path.basename(path)[:-4]}: {text}")

state["alarms"] = alarms
state["info"] = info

tmp_out = f"{out}.{os.getpid()}.tmp"     # per-pid so two runs cannot truncate each other
with open(tmp_out, "w") as f:
    json.dump(state, f, indent=1)
os.chmod(tmp_out, 0o644)
os.replace(tmp_out, out)      # atomic; readers never see a partial file
print(f"wrote {out}")
PY
