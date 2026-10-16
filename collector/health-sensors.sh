#!/bin/bash
# health-sensors.sh -- root-only snapshot of the FAST sensors (fans, chassis
# temperatures, power) for the dashboard, once a minute. health-collect.sh does
# the slow, heavy things (SMART, SEL, journal) every 15 minutes; this one exists
# because a power bar or fan chart that is 15 minutes old is useless, and the web
# process must never have root to read IPMI itself.
#
# Same rules as health-collect.sh: every capture's stderr and non-zero exit go to
# a .err sidecar, which becomes a `collector:` alarm (a partial snapshot must not
# read as healthy); output is written to a per-pid temp file and renamed into
# place, mode 0644, so the dashboard (an ordinary user) reads it without privileges. The
# alarms here (sensor status nc/cr/nr, fan ceiling, GPU thermals and ECC) are printed
# by the dashboard's Hardware card while this snapshot is fresh (a stale one is shown
# as unknown, not clear) and by the MOTD script, alongside state.json's.
#
# Sources:
#   - ipmitool sdr type fan / temperature / current: the BMC's own readings. Dell
#     files "Pwr Consumption" (Watts) under type Current, next to the per-PSU
#     "Current N" (Amps) rows.
#   - Intel RAPL energy counters (/sys/class/powercap, root-only): two reads 1 s
#     apart give per-socket CPU and DRAM watts. A box without RAPL gets no rows
#     and no alarm.
#   - nvidia-smi: per-GPU core and memory (HBM) temperature, the driver's own
#     thermal limits, thermal slowdown state and counters, ECC. The core throttles
#     itself at its max operating temperature; the memory has no throttle of its
#     own, so it can run past its limit while the core looks fine. A box without
#     nvidia-smi gets no rows and no alarm.
#
# Usage: sudo systemctl start horatio-sensors.service
# (the timer runs it every 60 s). Optional argv: output path, for testing.

set -uo pipefail
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin

OUT="${1:-/var/lib/horatio/sensors.json}"

[ "$(id --user)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

mkdir --parents --mode 0755 "$(dirname "$OUT")"
tmp=$(mktemp --directory) || exit 1     # no set -e here; an empty $tmp would redirect into /
trap 'rm -rf "$tmp"' EXIT

# --- captures ----------------------------------------------------------------
# The plain `|` table (not `-c` CSV) has the same five columns for every sensor:
#   Fan1 | 30h | ok | 7.1 | 17640 RPM
# `|| echo "exit $?"` records a non-zero exit in the sidecar: `timeout` kills
# silently (124), so without it a wedged BMC would look like an empty table.
# No BMC (a laptop, a desktop): skip the IPMI captures instead of alarming; the
# python notes it once as `info`. The tool AND the device node: ipmitool
# installed on a box without a BMC fails every run with "Could not open device
# at /dev/ipmi0 ...", and that is not a fault.
if command -v ipmitool > /dev/null && [ -e /dev/ipmi0 ]; then
    timeout 60 ipmitool sdr type fan          > "$tmp/fan.txt"   2> "$tmp/fan.err"   || echo "exit $?" >> "$tmp/fan.err"
    timeout 60 ipmitool sdr type temperature  > "$tmp/temp.txt"  2> "$tmp/temp.err"  || echo "exit $?" >> "$tmp/temp.err"
    timeout 60 ipmitool sdr type current      > "$tmp/power.txt" 2> "$tmp/power.err" || echo "exit $?" >> "$tmp/power.err"
else
    for f in fan temp power; do : > "$tmp/$f.txt"; : > "$tmp/$f.err"; done
    : > "$tmp/no-bmc"
fi

# RAPL. First line: the read time (seconds, fractional); then one line per
# domain: "<path> <name> <energy_uj> <max_energy_range_uj>". package-N is CPU
# socket N; on the server its child domain (intel-rapl:N:0) is that socket's
# DRAM. Laptops expose core/uncore/psys children instead, which the python
# skips: they are parts of (or supersets of) the package and would double
# count. On a box without RAPL the glob matches nothing (the `continue` keeps
# the loop's status 0) and the python emits no rows.
rapl() {
    date +%s.%N
    for d in /sys/class/powercap/intel-rapl:*; do
        [ -e "$d" ] || continue
        echo "$d $(cat "$d/name") $(cat "$d/energy_uj") $(cat "$d/max_energy_range_uj")"
    done
}
rapl > "$tmp/rapl1.txt" 2> "$tmp/rapl.err"  || echo "exit $?" >> "$tmp/rapl.err"
sleep 1
rapl > "$tmp/rapl2.txt" 2>> "$tmp/rapl.err" || echo "exit $?" >> "$tmp/rapl.err"

# GPUs. The CSV is one line per GPU, in the order the query names the fields
# (the python's GPU_FIELDS); unreadable fields come back as "[N/A]", "N/A" or
# "[Not Supported]". The throttle counters are microseconds since driver load.
# The limits are not query fields, so they come from the -q text, one block per
# GPU headed by its PCI bus id (the CSV's pci.bus_id, which pairs the two).
# nvidia-smi installed but the driver not loaded fails both captures: a
# `collector:` alarm, on purpose. No nvidia-smi at all: no rows, no alarm.
if command -v nvidia-smi > /dev/null; then
    timeout 30 nvidia-smi --query-gpu=index,pci.bus_id,name,temperature.gpu,temperature.memory,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.hw_slowdown,clocks_event_reasons_counters.sw_thermal_slowdown,clocks_event_reasons_counters.hw_thermal_slowdown,ecc.errors.uncorrected.volatile.total,retired_pages.pending,power.draw,power.limit \
        --format=csv,noheader,nounits > "$tmp/gpu.txt" 2> "$tmp/gpu.err" || echo "exit $?" >> "$tmp/gpu.err"
    timeout 30 nvidia-smi -q -d TEMPERATURE > "$tmp/gpu-temp.txt" 2> "$tmp/gpu-temp.err" || echo "exit $?" >> "$tmp/gpu-temp.err"
else
    for f in gpu gpu-temp; do : > "$tmp/$f.txt"; : > "$tmp/$f.err"; done
fi

python3 - "$tmp" "$OUT" <<'PY'
import glob, json, os, sys, time

tmp, out = sys.argv[1], sys.argv[2]
alarms = []

def read(name):
    # Every file read here was written by a redirection in the bash stage above,
    # so a missing one is a bug in this script and should crash into the journal.
    with open(os.path.join(tmp, name)) as f:
        return f.read()

# Same per-machine config file as health-collect.sh; only fan_rpm_alarm_above
# and gpu_temp_alarm_margin_c are read here. A missing file means defaults; malformed JSON crashes on purpose.
try:
    with open(os.environ.get("HEALTH_CONFIG", "/etc/horatio/config.json")) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

state = {"ts": int(time.time()), "info": []}
if os.path.exists(os.path.join(tmp, "no-bmc")):
    state["info"].append("no BMC (ipmitool or /dev/ipmi0 missing): no BMC sensors")

# ---- IPMI sensors ------------------------------------------------------------
def sensors(text, what):
    # Every line is "name | id | status | entity | reading", e.g.
    #   "Fan Redundancy   | 75h | ok  |  7.1 | Fully Redundant"
    #   "Temp             | 0Eh | ok  |  3.1 | 32 degrees C"
    #   "Pwr Consumption  | 77h | ok  |  7.1 | 238 Watts"
    # `what` names the capture (fan/temp/power) in the alarm for a line that is
    # not five fields: a truncated capture or an informational line. Alarmed,
    # not raised, so one odd line never costs the rest of the snapshot.
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5:
            alarms.append(f"collector: {what}: unparsed line: {line.strip()!r}")
            continue
        name, _, status, entity, reading = parts
        # "17640 RPM" / "23 degrees C" -> number; "No Reading", "Fully Redundant" -> None.
        # Parsed here, once, so the dashboard never re-parses the string.
        try:
            value = float(reading.split()[0])
        except (ValueError, IndexError):
            value = None
        rows.append({"name": name, "status": status, "entity": entity, "reading": reading, "value": value})
    return rows

def sensor_alarms(rows):
    # Two sensors can share a name (Dell BMCs expose several plain "Temp"); the
    # entity id (e.g. 3.1) tells them apart. The label is stored on the row so
    # the dashboard keys its charts and history by the same string.
    dup = {r["name"] for r in rows if sum(x["name"] == r["name"] for x in rows) > 1}
    for r in rows:
        r["label"] = f"{r['name']} ({r['entity']})" if r["name"] in dup else r["name"]
        # `ns` = no reading (unpopulated slot / disabled sensor), normal on Dell;
        # nc / cr / nr are threshold crossings. Discrete sensors (Fan Redundancy,
        # PS Redundancy) are only ever ok/ns: "Redundancy Lost" shows in their
        # reading column and reaches alarms through the SEL patterns, not here.
        if r["status"] not in ("ok", "ns"):
            alarms.append(f"{r['label']}: status {r['status']}")

state["fans"] = sensors(read("fan.txt"), "fan")
state["temps"] = sensors(read("temp.txt"), "temp")
state["power"] = sensors(read("power.txt"), "power")
sensor_alarms(state["fans"])
sensor_alarms(state["temps"])
sensor_alarms(state["power"])

# `fan_rpm_alarm_above` in the config, for a BMC fan profile changed by hand
# (e.g. Dell's third-party PCIe fan boost turned off): the setting may not
# survive a power-cycle, and a silent revert to full speed is otherwise `status: ok`.
# It also fires while the fans are held at a manual speed above the ceiling, or
# when the BMC spins them up because the box is hot. Absent/null = off.
ceiling = config.get("fan_rpm_alarm_above")
if ceiling is not None:
    over = [r for r in state["fans"] if r["value"] is not None and r["value"] > ceiling]
    if over:
        alarms.append(f"fans: {len(over)} over {ceiling} RPM (boost back on, manual override, or box hot), "
                      f"e.g. {over[0]['label']} {int(over[0]['value'])}")

# ---- GPUs (nvidia-smi) ---------------------------------------------------------
# The --query-gpu fields of the bash stage, in order.
GPU_FIELDS = ["index", "bus_id", "name", "core_c", "mem_c", "sw_thermal", "hw_thermal", "hw_slowdown",
              "sw_thermal_us", "hw_thermal_us", "ecc_uncorrected", "retired_pending", "power_w", "power_limit_w"]

def num(s):
    # "83", "26.25" -> number; "[N/A]", "N/A", "[Not Supported]" -> None.
    try:
        return float(s)
    except ValueError:
        return None

def short_model(name):
    # "Tesla V100-PCIE-32GB" -> "V100", "Tesla P40" -> "P40", "NVIDIA RTX A6000" -> "RTX A6000":
    # the vendor / product-line words dropped, then the form factor and memory size.
    words = [w for w in name.split() if w not in ("NVIDIA", "Tesla", "GeForce", "Quadro")]
    return " ".join(words).split("-")[0] or name

def gpu_limits(text):
    # `nvidia-smi -q -d TEMPERATURE`: one block per GPU, headed by its bus id at
    # column 0 ("GPU 00000000:82:00.0"), then indented "Key : value" lines such as
    #   "        GPU Max Operating Temp            : 83 C"
    #   "        Memory Max Operating Temp         : N/A"
    # -> {bus_id: {shutdown, slowdown, max_op, mem_max_op}}, a value None when N/A.
    keys = {"GPU Shutdown Temp": "shutdown", "GPU Slowdown Temp": "slowdown",
            "GPU Max Operating Temp": "max_op", "Memory Max Operating Temp": "mem_max_op"}
    limits, cur = {}, None
    for line in text.splitlines():
        if line.startswith("GPU "):
            cur = limits.setdefault(line.split()[1].upper(), dict.fromkeys(keys.values()))
            continue
        key, _, value = line.partition(":")
        if cur is not None and key.strip() in keys:
            cur[keys[key.strip()]] = num(value.split()[0]) if value.split() else None
    return limits

def deg(x):
    return f"{x:g}"         # 86.0 -> "86"

limits = gpu_limits(read("gpu-temp.txt"))
# Absent = 3; null = no temperature rules (the slowdown, ECC and page rules still run).
margin = config.get("gpu_temp_alarm_margin_c", 3)
state["gpus"] = []
for line in read("gpu.txt").splitlines():
    if not line.strip():
        continue
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != len(GPU_FIELDS) or num(parts[0]) is None:
        alarms.append(f"collector: gpu: unparsed line: {line.strip()!r}")
        continue
    f = dict(zip(GPU_FIELDS, parts))
    n = int(num(f["index"]))
    # "Active" / "Not Active"; N/A counts as not active. sw is the driver's own
    # capping at max operating temp, hw the board's emergency halving of the clocks.
    # sw_power_cap is deliberately not here: an idle, capped card sits in it.
    throttle = [r for r in ("sw_thermal", "hw_thermal") if f[r] == "Active"]
    us = lambda k: round(num(f[k]) / 1e6, 1) if num(f[k]) is not None else None
    ecc = num(f["ecc_uncorrected"])
    row = {"index": n, "name": f["name"], "label": f"GPU {n} ({short_model(f['name'])})",
           "core_c": num(f["core_c"]), "mem_c": num(f["mem_c"]),
           "limits": limits.get(f["bus_id"].upper(), {"shutdown": None, "slowdown": None, "max_op": None, "mem_max_op": None}),
           "throttle": throttle, "hw_slowdown": f["hw_slowdown"] == "Active",
           "sw_thermal_s": us("sw_thermal_us"), "hw_thermal_s": us("hw_thermal_us"),
           "ecc_uncorrected": int(ecc) if ecc is not None else None,
           "retired_pending": {"Yes": True, "No": False}.get(f["retired_pending"]),
           "power_w": num(f["power_w"]), "power_limit_w": num(f["power_limit_w"])}
    state["gpus"].append(row)

    lim = row["limits"]
    if margin is not None:
        # The memory has no throttle of its own: the core can sit under its limit
        # while the memory is already past its own.
        for what, temp, top in (("memory", row["mem_c"], lim["mem_max_op"]), ("core", row["core_c"], lim["max_op"])):
            if temp is not None and top is not None and temp >= top - margin:
                alarms.append(f"GPU {n}: {what} {deg(temp)} C >= max operating {deg(top)} C - {deg(margin)}")
    if throttle:
        alarms.append(f"GPU {n}: thermal slowdown active ({', '.join(r.split('_')[0] for r in throttle)})")
    if row["ecc_uncorrected"]:
        alarms.append(f"GPU {n}: {row['ecc_uncorrected']} uncorrected ECC errors since driver load")
    if row["retired_pending"]:
        alarms.append(f"GPU {n}: retired pages pending")

# ---- RAPL: CPU and DRAM watts per socket --------------------------------------
# Emitted in the same row shape as the IPMI power rows, so the dashboard's
# power bar and the history recorder treat them like any other Watts reading.
def rapl(name):
    lines = read(name).splitlines()
    if not lines:
        return None, {}
    rows = {}
    for line in lines[1:]:
        parts = line.split()
        if len(parts) != 4:
            alarms.append(f"collector: rapl: unparsed line: {line.strip()!r}")
            continue
        rows[parts[0]] = parts
    return float(lines[0]), rows

t1, r1 = rapl("rapl1.txt")
t2, r2 = rapl("rapl2.txt")
if t1 is not None and t2 is not None and t2 > t1:
    for path, p1 in r1.items():
        p2 = r2.get(path)
        if p2 is None:
            continue
        energy1, wrap_at, energy2 = int(p1[2]), int(p1[3]), int(p2[2])
        delta = energy2 - energy1
        if delta < 0:                       # the counter wrapped between the reads
            delta += wrap_at + 1
        watts = round(delta / 1e6 / (t2 - t1), 1)
        socket = os.path.basename(path).split(":")[1]
        domain = p1[1]
        if not (domain.startswith("package") or domain == "dram"):
            continue                        # core/uncore/psys: inside or above the package
        label = f"CPU {socket}" if domain.startswith("package") else f"DRAM {socket}"
        state["power"].append({"name": label, "label": label, "status": "ok", "entity": "rapl",
                               "reading": f"{watts} Watts", "value": watts})

# ---- collection failures (any non-empty .err sidecar) ------------------------
for path in sorted(glob.glob(os.path.join(tmp, "*.err"))):
    text = read(os.path.basename(path)).strip()
    if text:
        alarms.append(f"collector: {os.path.basename(path)[:-4]}: {text}")

state["alarms"] = alarms

tmp_out = f"{out}.{os.getpid()}.tmp"     # per-pid so two runs cannot truncate each other
with open(tmp_out, "w") as f:
    json.dump(state, f, indent=1)
os.chmod(tmp_out, 0o644)
os.replace(tmp_out, out)      # atomic; readers never see a partial file
PY
