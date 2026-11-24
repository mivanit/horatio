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
#     whose description matches the fault phrases in the config file. No SEL state
#     is kept: an event counts as "recent" when the BMC's timestamp is within 7 days.
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
# No state is kept between runs: a snapshot says what is wrong NOW. Each alarm
# carries a stable key (`alarm_items`) and the snapshot says what it really
# checked (`checked`), so that the dashboard server, which does the remembering,
# can tell since when an alarm has been there and when it went (its
# logs/events.jsonl; see collector/README.md).
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
# No BMC (a laptop, a desktop): skip the IPMI capture instead of alarming; the
# python notes it once as `info`. The tool AND the device node: ipmitool
# installed on a box without a BMC fails every run with "Could not open device
# at /dev/ipmi0 ...", and that is not a fault. `command -v` is a presence
# test, its stdout is the path, not an error.
if command -v ipmitool > /dev/null && [ -e /dev/ipmi0 ]; then
    timeout 180 ipmitool sel list          > "$tmp/sel.txt"  2> "$tmp/sel.err"  || echo "exit $?" >> "$tmp/sel.err"
else
    : > "$tmp/sel.txt"; : > "$tmp/sel.err"; : > "$tmp/no-bmc"
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
# The previous boot: its last journal lines (a clean shutdown ends with journald's
# own "Journal stopped"; a power cut or a hang ends mid-sentence) and its kernel.
# Reads from the end / the start of that boot, so it costs nothing however long
# the boot was. Not .err sidecars on purpose: a journal that holds only the
# current boot makes journalctl fail, and that is a fact, not a collector fault.
timeout 30 journalctl --boot -1 --lines 4 --quiet --no-pager --output short-iso \
                                             > "$tmp/prev-boot.txt" 2> /dev/null || : > "$tmp/prev-boot.txt"
timeout 30 journalctl --boot -1 --dmesg --lines +1 --quiet --no-pager --output cat \
                                             > "$tmp/prev-boot-kernel.txt" 2> /dev/null || : > "$tmp/prev-boot-kernel.txt"
# Ask the TIMER, never `systemctl show system-backup.service`: a oneshot that is
# not running is unloaded and `show` prints defaults for it. With
# --timestamp=unix (systemd >= 247) LastTriggerUSec is "@<epoch>", or empty when
# the timer never fired. Some versions (seen on 255) ignore the flag for `show`
# and print a date like "Tue 2026-11-17 03:10:05 UTC", so date(1) converts
# either form to epoch seconds. A value it cannot parse is an error in the
# sidecar (a collector alarm), never a silent "never fired".
# The timer's name is the config's `backup_timer` (absent or null = this box has
# no backup timer: no section, no alarm). Chosen here, once,
# and handed to the python via backup-timer.name. A config that does not parse
# lands in the sidecar (and is the python stage's `load_config crashed` alarm).
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
    # TZ=UTC: the date form is then always "... UTC", never a zone abbreviation
    # date(1) would have to guess at.
    last=$(TZ=UTC timeout 60 systemctl show "$BACKUP_TIMER" --property LastTriggerUSec --value --timestamp=unix 2>> "$tmp/backup-timer.err") \
        || echo "exit $?" >> "$tmp/backup-timer.err"
    if [ -n "$last" ]; then
        last=$(date --date="$last" +%s 2>> "$tmp/backup-timer.err") \
            || { echo "exit $?" >> "$tmp/backup-timer.err"; last=; }
    else
        # `show` on a unit that does not exist also prints nothing, exit 0. A
        # mistyped name must be an alarm at once, not "has not fired since boot".
        load=$(timeout 60 systemctl show "$BACKUP_TIMER" --property LoadState --value 2>> "$tmp/backup-timer.err") \
            || echo "exit $? (LoadState)" >> "$tmp/backup-timer.err"
        [ "$load" != not-found ] || echo "no such timer: $BACKUP_TIMER" >> "$tmp/backup-timer.err"
    fi
    echo "$last" > "$tmp/backup-timer.txt"
fi

# smartctl's exit status is a BITMASK, so the python step decodes it:
#   bits 0-1 (1, 2)     could not read the drive: bad command line, open or
#                       identify failed
#   exit 3              `--nocheck=standby,3`: the drive was in standby and was LEFT
#                       ASLEEP (3 is the status we ask for; it happens to be
#                       bits 0+1, so it is checked before the bits are)
#   bit 2 (4)           a SMART command failed; an optional failure can leave
#                       usable readings, while a missing smart_status makes the
#                       drive count as unreadable
#   bit 3 (8)           SMART health check says the drive is FAILING
#   bits 4-6 (16..64)   attributes at/under threshold now or in the past,
#                       entries in the error log
#   bit 7 (128)         the self-test log has errors: a scheduled test failed
# `timeout` exits 124 with no JSON at all, which the missing smart_status catches.
# SATA/SAS/USB disks and NVMe namespaces; smartctl speaks both. No smartmontools
# at all is an ALARM (the marker file below), not a quiet gap: every box has drives.
# Per-drive limit: config `smart_timeout_s`, default 60. Drives run one at a time, so
# the run's worst case grows with the drive count (horatio-collect.service has the sum).
# A missing key or a value that is not a positive number means the default.
read -r SMART_TIMEOUT < <(python3 -c '
import json, sys
try:
    config = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    config = {}
value = config.get("smart_timeout_s")
ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
print(value if ok else 60)
' "${HEALTH_CONFIG:-/etc/horatio/config.json}")
: "${SMART_TIMEOUT:=60}"
if command -v smartctl > /dev/null; then
    for dev in /dev/sd* /dev/nvme*n*; do
        [ -b "$dev" ] || continue     # no match leaves the literal glob
        name=$(basename "$dev")
        # Whole disks only, however many there are (sdaa, nvme10n1): not sda1, nvme0n1p1.
        [[ $name =~ ^(sd[a-z]+|nvme[0-9]+n[0-9]+)$ ]] || continue
        timeout "$SMART_TIMEOUT" smartctl --nocheck=standby,3 --all --json "$dev" > "$tmp/smart_$name.json" 2> "$tmp/smart_$name.err"
        echo $? > "$tmp/smart_$name.rc"
    done
else
    : > "$tmp/no-smartctl"
fi

python3 - "$tmp" "$OUT" <<'PY'
import glob, hashlib, json, math, os, re, sys, time, traceback

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
# Identity for each alarm, parallel to `alarms` (same order, same text): the
# strings carry counts and ages that change from run to run, so a reader (the
# dashboard server's alarm history) cannot tell "the same alarm, still there"
# from a new one without a key.
alarm_items = []
# Key prefixes this run really evaluated, with a clean capture. An alarm that is
# gone and whose prefix is NOT listed is unknown, not cleared: a crashed section,
# a timed-out tool, a drive left in standby or gone from /dev lists nothing.
# A prefix P covers key K when K == P or K starts with P + "/".
checked = []
# Keys of alarms that exist but are not printed (journal messages past the limit).
alarm_hidden = []

def alarm(key, text, value=None, requires=None):
    """Raise one alarm. `text` is what readers print and is all `alarms` holds.
    `key` is its stable identity: no counts, ages or device letters that move
    (drives go by serial). `value` only for a fault counter that never falls on
    its own (reallocated sectors, grown defects, ECC counts), so a reader can
    report 8 -> 9; temperatures and ages get none. A key raised twice in one
    run gets #2, #3. Never raises: it is called from part()'s except handler."""
    alarms.append(text)
    item = {"key": f"unkeyed/{len(alarm_items)}", "text": text}
    try:
        used = {i["key"] for i in alarm_items}
        k, n = str(key), 2
        while k in used:
            k, n = f"{key}#{n}", n + 1
        item["key"] = k
        if value is not None:
            item["value"] = value
        if requires is not None:
            item["requires"] = requires
    except Exception:
        traceback.print_exc()
    alarm_items.append(item)

def check(*prefixes):
    for p in prefixes:
        if p not in checked:
            checked.append(p)

def read(name):
    # Every file read here was written by a redirection in the bash stage above,
    # so a missing one is a bug in this script: it crashes that section (part()).
    with open(os.path.join(tmp, name)) as f:
        return f.read()

def sysread(*parts):
    """One stripped line from a sysfs file, or None if it does not exist."""
    try:
        with open(os.path.join(*parts)) as f:
            return f.read().strip()
    except OSError:
        return None

def number(value):
    """A measured scalar, never an absent field, boolean or nonfinite value."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def counter(value):
    """A nonnegative sysfs counter, or None when it could not be read."""
    try:
        n = int(value)
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None

def capture_ok(name):
    """True when the bash stage's capture <name> ran clean (empty .err sidecar)."""
    text = read(f"{name}.err").strip()
    return not text or text == "SEL has no entries"

# schema: bumped when the meaning of alarm_items / checked changes. boot_id tells
# a reader that counters which reset at boot (ECC, failed units) did so.
state = {"schema": 1, "ts": int(time.time()),
         "boot_id": sysread(os.environ.get("HEALTH_BOOT_ID", "/proc/sys/kernel/random/boot_id"))}
crashed = []

def part(section):
    """Run one section now. A crash costs only that section: it becomes a
    `collector: <section> crashed` alarm and its traceback goes to the journal,
    every other section still reports, and the run exits 1 after writing the
    snapshot so the unit's failure mail still goes out. Without this, one bad
    value anywhere left the whole snapshot stale, every other alarm with it."""
    try:
        section()
        check(f"collector/{section.__name__}")
    except Exception as e:
        crashed.append(section.__name__)
        alarm(f"collector/{section.__name__}", f"collector: {section.__name__} crashed: {type(e).__name__}: {e}")
        traceback.print_exc()

if os.path.exists(os.path.join(tmp, "no-smartctl")):
    alarm("smart-tools", "smartmontools not installed: drives are not monitored")
else:
    check("smart-tools")
# Absent hardware is absent, not zero: with no BMC the two SEL sections write
# null (the dashboard then shows no "BMC log" rows) and this one note says why.
NO_BMC = os.path.exists(os.path.join(tmp, "no-bmc"))
if NO_BMC:
    info.append("no BMC (ipmitool or /dev/ipmi0 missing): no event log")

# ---- config (per-machine SEL fault policy) -----------------------------------
# The SEL logs real faults and routine chatter under the same sensor types, and
# which events count as faults is deployment-specific (a lost PSU is a fault on
# a dual-PSU box, normal on a single-PSU one; chassis intrusion is usually
# noise). So the fault phrases live in a config file, editable without touching
# this script. Matched case-insensitively as substrings of the event
# description. A missing file uses the built-in default; malformed JSON is a
# `collector: load_config crashed` alarm, and the sections that need the config
# (SEL hardware faults, drives) crash with it instead of guessing.
SEL_FAULT_PATTERNS_DEFAULT = [
    "AC lost", "Redundancy Lost", "Lower Critical going", "Upper Critical going",
    "Drive Fault", "failure", "failed",
]
# These two only make alarms quieter, so with a config that cannot be read they
# fall back to "nothing accepted, nothing ignored" instead of crashing the
# drives and journal sections with it.
smart_baseline, journal_ignore = {}, []
def load_config():
    global sel_fault_patterns, grown_defect_baseline, smart_baseline, journal_ignore
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
    # The same idea for the ATA attributes below: {serial: {"reallocated": 8}} accepts
    # what a drive already has, and the alarm returns when the count grows past it.
    smart_baseline = config.get("smart_baseline", {})
    journal_ignore = config.get("journal_ignore", [])
part(load_config)

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

def sel_read():
    """True when the SEL was really read this run: there is a BMC and ipmitool ran clean."""
    return not os.path.exists(os.path.join(tmp, "no-bmc")) and capture_ok("sel")

def sel_memory():
    if NO_BMC:
        state["sel_memory"] = None
        return
    sel_lines = read("sel.txt").splitlines()
    mem = [l.strip() for l in sel_lines if "mem" in l.lower() or "ecc" in l.lower()]
    recent = [l for l in mem if sel_recent(l)]
    state["sel_memory"] = {"count": len(mem), "recent": recent}
    if recent:
        alarm("sel/memory", f"SEL: {len(recent)} memory events in {SEL_RECENT_DAYS}d ({len(mem)} total), latest: {recent[-1]}")
    if sel_read():
        check("sel/memory")
part(sel_memory)

# ---- SEL, hardware faults (PSU / fan / drive / thermal) ----------------------
# The non-memory faults from the same log. A real 6-column row whose description
# (col 5) matches a configured fault phrase and whose direction (col 6) is
# "Asserted" -- the onset, not a "Deasserted" recovery. Everything else (chassis
# intrusion, presence detected, redundancy REGAINED, non-critical warnings) just
# does not match. Memory lines are the mem/ecc slice above and are skipped here;
# they also carry an embedded "|" in the description, so their column count and
# direction position differ -- another reason not to run them through this.
def sel_hardware():
    if NO_BMC:
        state["sel_hardware"] = None
        return
    sel_lines = read("sel.txt").splitlines()

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
        alarm("sel/hardware", f"SEL: {len(hw_recent)} hardware events in {SEL_RECENT_DAYS}d ({len(hw)} total), latest: {hw_recent[-1]}")

    # A dated-looking line whose date did not parse is a format change (locale, ipmitool
    # version), not a hardware event: say so instead of silently under-counting.
    unparsed = [l.strip() for l in sel_lines
                if "|" in l and "Pre-Init" not in l and "OEM record" not in l and sel_when(l) is None]
    if unparsed:
        alarm("sel/dates", f"collector: {len(unparsed)} SEL lines with unrecognised date format, e.g. {unparsed[-1]}")
    if sel_read():
        check("sel/hardware", "sel/dates")
part(sel_hardware)

# ---- EDAC per-DIMM (no privileges needed; counters RESET EVERY BOOT) ---------
# HEALTH_EDAC / HEALTH_UPTIME point the fixture run at a fake tree (README).
# No DIMMs under EDAC means memory errors are not being counted at all (non-ECC
# memory, or ECC memory without its EDAC driver), so `edac` is null and one info
# note says so; a "0 corrected" line would read as healthy ECC.
def edac():
    dimms = []
    for d in sorted(glob.glob(os.path.join(os.environ.get("HEALTH_EDAC", "/sys/devices/system/edac/mc"), "mc*/dimm*"))):
        label = sysread(d, "dimm_label")
        if label is None:
            continue
        dimms.append({
            "path": "/".join(d.split("/")[-2:]),
            "label": label,          # generic sb_edac name, NOT Dell A1..B12
            "ce_count": counter(sysread(d, "dimm_ce_count")),
            "ue_count": counter(sysread(d, "dimm_ue_count")),
        })
    if not dimms:
        state["edac"] = None
        info.append("no ECC reporting (no DIMMs under EDAC): memory errors are not monitored")
        return
    uptime = int(float(open(os.environ.get("HEALTH_UPTIME", "/proc/uptime")).read().split()[0]))
    def total(field):
        values = [d[field] for d in dimms]
        return sum(values) if all(v is not None for v in values) else None
    ce_total, ue_total = total("ce_count"), total("ue_count")
    state["edac"] = {
        "uptime_seconds": uptime,
        "dimms": dimms,
        "ce_total": ce_total,
        "ue_total": ue_total,
    }
    for d in dimms:
        key = f"ecc/dimm/{d['path']}/uncorrectable"
        if d["ue_count"] is not None:
            check(key)
            if d["ue_count"] > 0:
                alarm(key, f"ECC {d['label']}: {d['ue_count']} uncorrectable errors since boot", d["ue_count"])
    # Corrected errors retain their aggregate threshold. Unknown counters cannot
    # establish recovery, and an uncorrectable fault takes precedence.
    if ue_total == 0 and ce_total is not None:
        requires = [f"ecc/dimm/{d['path']}/correctable" for d in dimms]
        check(*requires)
        if ce_total >= CE_WARN:
            alarm("ecc/correctable", f"ECC: {ce_total} correctable errors since boot", ce_total, requires=requires)

part(edac)

# ---- drives ------------------------------------------------------------------
def smart():
    drives = state["drives"] = []

    def drive(name):
        rc = int(read(f"smart_{name}.rc"))
        if rc == 3:                              # standby: left asleep, nothing read
            drives.append({"device": name, "standby": True})
            info.append(f"{name}: standby")
            return

        try:
            with open(os.path.join(tmp, f"smart_{name}.json")) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        # Bits 0-1 (bad args, open/identify failed) mean nothing was read. No `smart_status`
        # in the JSON means smartctl died before printing it (`timeout`, exit 124) or SMART
        # is disabled/unsupported (bit 2 on a mandatory command). Bit 2 alone, with the
        # JSON present, can leave other successfully read fields usable.
        if rc & 3 or "smart_status" not in s:
            msgs = [m.get("string") for m in (s.get("smartctl") or {}).get("messages") or []]
            why = read(f"smart_{name}.err").strip() or "; ".join(m for m in msgs if m) \
                  or f"smartctl exit {rc}, no JSON"
            drives.append({"device": name, "error": why})
            alarm(f"smart/dev:{name}/unreadable", f"{name}: unreadable ({why})")
            return

        serial = s.get("serial_number")
        # Alarm keys go by serial: device letters move between boots. A drive that
        # gave no serial, or no report at all (above), can only go by its letter.
        did = f"smart/{serial}" if serial else f"smart/dev:{name}"
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
        # Each rule is `checked` only when what it reads was really in the report:
        # a drive that answered with its health but not its attribute table has
        # not been cleared of anything the table would show.
        if isinstance(entry["passed"], bool):
            check(f"{did}/health")
        if entry["passed"] is False:
            alarm(f"{did}/health", f"{name}: SMART health FAILED")
        # NVMe has no attribute table; its health log is the whole story. `passed`
        # already covers critical_warning (spare below threshold, over temperature,
        # read-only, ...); media errors and wear-out are separate counters.
        nvme = s.get("nvme_smart_health_information_log") or {}
        if nvme:
            entry["media_errors"] = nvme.get("media_errors")
            entry["percentage_used"] = nvme.get("percentage_used")
            for field in ("media_errors", "percentage_used"):
                if number(nvme.get(field)):
                    check(f"{did}/{field}")
            if number(nvme.get("media_errors")) and nvme["media_errors"] > 0:
                alarm(f"{did}/media_errors", f"{name}: media_errors={nvme['media_errors']}", nvme["media_errors"])
            if number(nvme.get("percentage_used")) and nvme["percentage_used"] >= 100:
                alarm(f"{did}/percentage_used", f"{name}: percentage_used={nvme['percentage_used']} (endurance spent)")
        if rc & 128:    # bit 7: the self-test log has errors. ATA drops entries outdated by
                        # a later successful extended test; SCSI alarms while any failed
                        # entry is in its 20-entry log.
            alarm(f"{did}/selftest", f"{name}: self-test log has errors")
        # A failed optional command may be the self-test log read: no error bit
        # then means unknown, not healthy. A positive finding is still evidence.
        if rc & 128 or not rc & 4:
            check(f"{did}/selftest")
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
        if number(trip):
            entry["drive_trip_c"] = trip
            if number(entry["temperature_c"]):
                check(f"{did}/temperature")
                if entry["temperature_c"] >= trip - 5:
                    alarm(f"{did}/temperature", f"{name}: {entry['temperature_c']}C, trip point {trip}C")

        # SAS: the grown defect list is the signal that matters. A SAS drive can
        # fail with every error counter at zero to the end; only this number and
        # the health bit move.
        if "scsi_grown_defect_list" in s:
            grown = s["scsi_grown_defect_list"]
            entry["grown_defect_list"] = grown
            base = grown_defect_baseline.get(serial, 0)
            entry["grown_defect_baseline"] = base
            if number(grown):
                check(f"{did}/grown_defects")
            if number(grown) and grown > base:
                alarm(f"{did}/grown_defects", f"{name} ({serial}): grown defects {grown} > baseline {base}", grown)

        # SATA: the attributes that actually indicate media failure.
        got = {}
        for a in (s.get("ata_smart_attributes") or {}).get("table") or []:
            if a.get("id") in ATA_WATCH:
                got[ATA_WATCH[a["id"]]] = (a.get("raw") or {}).get("value")
        if got:
            entry["attrs"] = got
            # One alarm per attribute: each has its own count and its own history.
            accepted = smart_baseline.get(serial) if isinstance(smart_baseline, dict) else None
            accepted = accepted if isinstance(accepted, dict) else {}
            for k, v in got.items():
                if number(v):
                    check(f"{did}/{k}")
                base = accepted.get(k)
                base = base if isinstance(base, int) and not isinstance(base, bool) and base > 0 else 0
                if k in ATA_ALARM and number(v) and v > base:
                    alarm(f"{did}/{k}", f"{name}: {k}={v}" + (f" (baseline {base})" if base else ""), v)
            if accepted:
                entry["attrs_baseline"] = {k: accepted[k] for k in got if k in accepted}

        drives.append(entry)
        # The drive answered: an earlier "unreadable" or crash under this device
        # letter may now count as gone.
        check(f"smart/dev:{name}/unreadable", f"smart/dev:{name}/crash")

    # One drive at a time: a crash on one must not leave the drives after it
    # unchecked, so it is that drive's alarm and error row and the loop goes on.
    for path in sorted(glob.glob(os.path.join(tmp, "smart_*.rc"))):     # sdX and nvmeXnY
        name = os.path.basename(path)[6:-3]      # smart_<name>.rc
        try:
            drive(name)
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            crashed.append("smart")
            alarm(f"smart/dev:{name}/crash", f"collector: smart crashed: {name}: {why}")
            drives.append({"device": name, "error": f"collector crashed ({why})"})
            traceback.print_exc()
part(smart)

# ---- what each drive holds (lsblk, no privileges) ---------------------------
# Physical drives are not mounts: md0 is two drives, / is an LVM volume on a
# partition. The lsblk tree repeats md0 under each member, so recursing from a
# disk down through its children collects every mountpoint it contributes to.
# An lsblk failure leaves the file empty; the .err sidecar alarms for it.
def drive_holds():
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
    for d in state.get("drives", []):     # none when the smart section crashed
        d["holds"] = sorted(holds.get(d["device"], ()))
part(drive_holds)

# ---- md arrays (sysfs, no privileges) ----------------------------------------
# HEALTH_SYSBLOCK exists only so a fake tree can be pointed at for testing.
def md_arrays():
    sysblock = os.environ.get("HEALTH_SYSBLOCK", "/sys/block")
    mds = []
    for md in sorted(glob.glob(os.path.join(sysblock, "md*", "md"))):
        name = os.path.basename(os.path.dirname(md))
        members = []
        for dev in sorted(glob.glob(os.path.join(md, "dev-*"))):
            member = os.path.basename(dev)[4:]                     # dev-<name>
            # sysfs prints e.g. "in_sync,writemostly"; keep it as a list.
            raw_flags = sysread(dev, "state")
            flags = [f for f in (raw_flags or "").split(",") if f]
            merrs = counter(sysread(dev, "errors"))
            if flags:
                check(f"md/{name}/member/{member}/state")
            if merrs is not None:
                check(f"md/{name}/member/{member}/errors")
            members.append({"name": member, "state": flags, "errors": merrs})
            for f in flags:
                if f in MD_BAD_STATES:
                    alarm(f"md/{name}/member/{member}/state/{f}", f"{name}: {member} state {f}")
            if merrs:
                alarm(f"md/{name}/member/{member}/errors", f"{name}: {member} errors={merrs}", merrs)
        degraded = counter(sysread(md, "degraded"))
        sync_action = sysread(md, "sync_action")
        array_state = sysread(md, "array_state")
        # Blocks that differed between mirrors at the last `check`; persists until
        # the next check. On RAID1 a small count can be benign (in-flight writes).
        # It climbs while a check/resync/repair RUNS, so only judge it once idle.
        mismatch = counter(sysread(md, "mismatch_cnt"))
        mds.append({"name": name, "level": sysread(md, "level"), "degraded": degraded,
                    "sync_action": sync_action, "array_state": array_state,
                    "mismatch_cnt": mismatch, "members": members})
        if degraded is not None:
            check(f"md/{name}/degraded")
        if array_state:
            check(f"md/{name}/array_state")
        if sync_action == "idle" and mismatch is not None:      # not judged while a check or resync runs
            check(f"md/{name}/mismatch")
            if mismatch:
                alarm(f"md/{name}/mismatch", f"{name}: mismatch_cnt={mismatch}", mismatch)
        if degraded:
            alarm(f"md/{name}/degraded", f"{name}: DEGRADED ({degraded} missing)")
        if array_state in ("inactive", "readonly"):
            alarm(f"md/{name}/array_state", f"{name}: array_state {array_state}")
        if sync_action in ("check", "resync", "repair", "recover", "reshape"):
            info.append(f"{name}: {sync_action} in progress")
    state["md"] = mds
part(md_arrays)

# ---- backup ------------------------------------------------------------------
# Only "did the timer fire recently". A run that FAILS stays in systemd's failed
# state until the next start, so it already shows up as `unit failed:` below,
# and ExecStopPost= in the unit mails the log.
def backup():
    backup_timer = read("backup-timer.name").strip()     # chosen by the bash stage from the config
    if backup_timer:
        last = read("backup-timer.txt").strip()      # epoch seconds, or empty: never fired
        last_trigger = int(last) if last else None
        age_h = round((state["ts"] - last_trigger) / 3600, 1) if last_trigger else None
        state["backup"] = {"timer": backup_timer, "last_trigger": last_trigger, "age_hours": age_h}
        if age_h is None:
            # systemd remembers the last trigger across a reboot only for
            # Persistent=true timers, so soon after boot "empty" proves nothing.
            uptime_h = float(open(os.environ.get("HEALTH_UPTIME", "/proc/uptime")).read().split()[0]) / 3600
            if uptime_h < BACKUP_MAX_AGE_H:
                info.append(f"backup: {backup_timer} has not fired since boot ({int(uptime_h)}h ago)")
                return               # nothing known yet: an earlier backup alarm is not cleared
            alarm("backup/never", f"backup: {backup_timer} has never fired")
        elif age_h > BACKUP_MAX_AGE_H:
            alarm("backup/overdue", f"backup: last trigger {int(age_h)}h ago")
    else:
        state["backup"] = None       # config says this box has no backup timer
    if capture_ok("backup-timer"):
        check("backup")
part(backup)

# ---- the 60 s sensors snapshot (health-sensors.sh) ---------------------------
# Its unit sends no failure mail (one per minute would be noise), so this run is
# what notices a dead sensors timer and carries it to the dashboard's Hardware
# card and the MOTD. Nothing is mailed for it: this unit mails only when it fails.
def sensors_snapshot():
    sensors_path = os.path.join(os.path.dirname(out), "sensors.json")
    try:
        sensors_age = int(time.time() - os.stat(sensors_path).st_mtime)
    except FileNotFoundError:
        sensors_age = None
    if sensors_age is None:
        alarm("sensors-snapshot/missing", "collector: no sensors snapshot (horatio-sensors.timer never ran?)")
    elif sensors_age > 300:
        alarm("sensors-snapshot/stale", f"collector: sensors snapshot {sensors_age}s old (horatio-sensors.timer stuck?)")
    check("sensors-snapshot")
part(sensors_snapshot)

# ---- failed systemd units ----------------------------------------------------
# Each line is "UNIT LOAD ACTIVE SUB DESCRIPTION"; only the unit name is kept.
def failed_units():
    state["failed_units"] = [l.split()[0] for l in read("failed.txt").splitlines() if l.strip()]
    for unit in state["failed_units"]:
        alarm(f"unit/{unit}", f"unit failed: {unit}")
    if capture_ok("failed"):
        check("unit")
part(failed_units)

# ---- journal errors (last 24 h) ----------------------------------------------
# Known-benign noise: ACPI IPMI region errors at every boot (a firmware quirk
# of e.g. Dell R730 boards), an empty kernel line, and sudo without a password (typos, and
# non-interactive sessions probing with sudo: "a password is required"). What
# is noise on one box only (a driver that announces itself at error level on
# every boot) goes in the config's `journal_ignore`, a list of regexes searched
# in the whole line; find candidates with `journalctl --priority err --since
# -7days` on the box.
JOURNAL_MAX = 8      # distinct messages shown one by one; past that, a count
def journal():
    JOURNAL_IGNORE = re.compile(r"ACPI Error|kernel:\s*$|pam_unix\(sudo:auth\)|a password is required")
    # Ubuntu's login PAM configuration can still name the removed lastlog
    # module. Ignore only that missing-module pair, not other PAM failures.
    lastlog = r"(?:/(?:usr/)?lib(?:64)?/(?:[^/\s:]+/)?security/)?pam_lastlog\.so"
    LOGIN_LASTLOG = re.compile(
        r"^\S+ \S+ login(?:\[\d+\])?:\s+PAM (?:"
        rf"unable to dlopen\({lastlog}\): {lastlog}: cannot open shared object file: No such file or directory"
        rf"|adding faulty module: {lastlog})\s*$"
    )
    ignore = [JOURNAL_IGNORE, LOGIN_LASTLOG]
    for pattern in journal_ignore if isinstance(journal_ignore, list) else [journal_ignore]:
        try:
            ignore.append(re.compile(pattern))
        except (re.error, TypeError) as e:      # a typo must not cost the journal section
            info.append(f"journal_ignore: skipped {pattern!r}: {e}")
    # short-iso: one line per message starting with its ISO timestamp, continuation
    # lines indented. The "timestamp host unit[pid]:" prefix is three tokens; sudo
    # pads its message with spaces after that, hence the strip.
    JOURNAL_PREFIX = re.compile(r"^\S+ \S+ \S+ ")
    lines = [l for l in read("journal-err.txt").splitlines()
             if re.match(r"\d{4}-\d\d-\d\dT", l) and not any(p.search(l) for p in ignore)]
    msgs = [JOURNAL_PREFIX.sub("", l).strip() for l in lines]
    distinct = set(msgs)
    state["journal_errors"] = {"hours": 24, "count": len(msgs), "distinct": sorted(distinct)}

    # One alarm per distinct message, so an important one is never hidden behind
    # "latest: <some other line>". The text says who logged it; digit runs (pids,
    # counts, addresses) do not make two messages different.
    groups = {}
    for l in lines:
        m = re.match(r"(\S+) \S+ (\S+?)(?:\[\d+\])?: ?(.*)", l)
        when = l.split()[0][:16].replace("T", " ")
        who, msg = (m.group(2), m.group(3).strip()) if m else ("?", JOURNAL_PREFIX.sub("", l).strip())
        said = msg if msg.startswith(f"{who}:") else f"{who}: {msg}"
        digest = hashlib.sha1(re.sub(r"\d+", "#", said).encode()).hexdigest()[:8]
        g = groups.setdefault(digest, {"n": 0, "first": when})
        g.update(n=g["n"] + 1, last=when, said=said)
    # Oldest first: the order, and so which ones are shown, does not change as
    # new messages arrive. One transient error keeps its line for 24 h: intended.
    shown = sorted(groups.items(), key=lambda kv: kv[1]["first"])
    for digest, g in shown[:JOURNAL_MAX]:
        when = f"at {g['first']}" if g["n"] == 1 else f"first {g['first']}, last {g['last']}"
        alarm(f"journal/{digest}", f"journal: {g['said']} ({g['n']} in 24h, {when})")
    if len(shown) > JOURNAL_MAX:
        alarm("journal-more", f"journal: {len(shown) - JOURNAL_MAX} more distinct error-level messages in 24h")
    # The messages past the ones printed are still there: a reader that knew one
    # of them from an earlier run must not take it for gone.
    alarm_hidden.extend(f"journal/{digest}" for digest, g in shown[JOURNAL_MAX:])
    if capture_ok("journal-err"):
        check("journal", "journal-more")
part(journal)

# ---- collection failures (any non-empty .err sidecar) ------------------------
# A tool that failed or timed out means this snapshot is partial, which must not
# read as healthy: it is an alarm, not a footnote. One exception: ipmitool says
# "SEL has no entries" on stderr (exit 0) when the log is empty, which is a fact,
# not a failure. The smart_*.err files belong to the drives stage above: an
# unreadable drive is already the rich `sdX: unreadable (...)` alarm, and stderr
# chatter from a drive that answered is deliberately not one.
def err_sidecars():
    for path in sorted(glob.glob(os.path.join(tmp, "*.err"))):
        if os.path.basename(path).startswith("smart_"):
            continue
        text = read(os.path.basename(path)).strip()
        if text and text != "SEL has no entries":
            name = os.path.basename(path)[:-4]
            alarm(f"capture/{name}", f"collector: {name}: {text}")
    check("capture")
part(err_sidecars)

# ---- boots ---------------------------------------------------------------------
# Which boot this is and how the previous one ended. `previous` is None when the
# journal does not reach back that far. An unclean end is a fact worth a day's
# note, not an alarm: nothing is wrong now.
def boot():
    lines = [l for l in read("prev-boot.txt").splitlines() if re.match(r"\d{4}-\d\d-\d\dT", l)]
    previous = None
    if lines:
        kernel = re.match(r"Linux version (\S+)", read("prev-boot-kernel.txt"))
        previous = {"clean": any("Journal stopped" in l for l in lines),
                    "ended": lines[-1].split()[0],
                    "kernel": kernel.group(1) if kernel else None}
    uptime = int(float(open(os.environ.get("HEALTH_UPTIME", "/proc/uptime")).read().split()[0]))
    state["boot"] = {"id": state["boot_id"], "kernel": os.uname().release, "previous": previous}
    if previous and not previous["clean"] and uptime < 86400:
        info.append(f"boot: the previous boot ended without a shutdown, last log line {previous['ended']}")
part(boot)

state["alarms"] = alarms
state["alarm_items"] = alarm_items
state["alarm_hidden"] = alarm_hidden
state["checked"] = checked
state["info"] = info

tmp_out = f"{out}.{os.getpid()}.tmp"     # per-pid so two runs cannot truncate each other
with open(tmp_out, "w") as f:
    json.dump(state, f, indent=1)
os.chmod(tmp_out, 0o644)
os.replace(tmp_out, out)      # atomic; readers never see a partial file
print(f"wrote {out}")
sys.exit(1 if crashed else 0)     # a failed run mails root (horatio-collect.service)
PY
