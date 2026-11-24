# collector — the root side of Horatio's hardware cards

The web process runs as an ordinary user and never touches `smartctl`, `ipmitool` or root-only
sysfs. Two small root scripts do, on systemd timers, and write JSON snapshots the dashboard (and,
if you want one, a login MOTD script) only read:

| Script | Every | Writes | What |
|---|---|---|---|
| `health-collect.sh` | 15 min | `/var/lib/horatio/state.json` | SMART per drive (serial, model, kind, size, temperature, verdict, SAS grown defects, what it holds), md arrays, EDAC counters, BMC event log (memory events + hardware faults), backup timer, failed units, journal errors, and the `alarms` / `info` lists |
| `health-sensors.sh` | 60 s | `/var/lib/horatio/sensors.json` | fans, chassis temperatures, PSU amps + BMC watts (IPMI), CPU and DRAM watts per socket (Intel RAPL), per-GPU core and memory temperature, the driver's thermal limits, thermal slowdown state and counters, ECC (`nvidia-smi`), and their `alarms`. Power rows carry `unit` (W/A/V) and `total` (true for the BMC's whole-machine reading: Dell "Pwr Consumption", or a name with "System Power" / "Total Power"), so no consumer guesses from names |

The collectors read only what needs root. Everything world-readable is read by the dashboard
itself: Linux hwmon temperatures, fans and power (`hwmon.py`, served as `/api/4/sensors`), and
the fan alarms from config.json `fans`, served in `/api/alarms` and shown on the
Hardware card next to the collectors' alarms.

```bash
sudo bash install.sh   # scripts -> /usr/local/bin, units -> /etc/systemd/system, config once
                       # (../install.sh --collector does this for you, and re-runs it on every update)
```

Root-owned copies run; the checkout is never executed by a timer, so editing it as a user
changes nothing until root re-runs the installer, which copies from it (so the web service's
unit mounts the checkout read-only, `logs/` aside). Idempotent, keeps an existing config.

## Requirements

- Linux with systemd, python3, `lsblk`, `journalctl`, and `smartmontools` for the drive rows
  (every whole `/dev/sd*` disk and `/dev/nvme*n*` namespace). Without smartmontools every run raises the alarm
  `smartmontools not installed: drives are not monitored` (the installer warns too).
- Optional: `ipmitool` and a BMC (`/dev/ipmi0`, e.g. Dell iDRAC). Without both, the BMC sections
  are skipped with one `info` note, no alarm. Optional: Intel RAPL (`/sys/class/powercap/intel-rapl:*`) for CPU /
  DRAM watts; absent means no rows. Optional: `nvidia-smi` for the `gpus` rows and GPU alarms; absent
  means no rows and no alarm (installed but failing, e.g. the driver not loaded, is a `collector:` alarm).
- A backup timer to watch, or `"backup_timer": null` in the config.

Cost: the 15-min run is a few seconds of mostly waiting (SMART answers come from the drive's
controller, a sleeping drive is left asleep); the 60 s run is three BMC table walks, a 1 s
RAPL sample and two `nvidia-smi` reads, ~0 CPU.

## Config: `/etc/horatio/config.json`

Installed once by `install.sh`, from `health-config.json` beside it (this box's copy,
gitignored) or, when that is absent, from `health-config.example.json`; then edit it in place,
it survives re-installs. Keys:

| key | meaning |
|---|---|
| `sel_fault_patterns` | case-insensitive substrings of BMC event descriptions that count as a hardware fault (`SEL: N hardware events in 7d`). Routine chatter is excluded by not being listed |
| `grown_defect_baseline` | `{serial: count}` for SAS drives; alarm when a drive exceeds its own number. An unlisted serial is watched from 0, so a replacement drive alarms as soon as it has defects: add it then |
| `smart_baseline` | `{serial: {attribute: count}}` for the ATA attributes that alarm (`reallocated`, `pending`, `offline_uncorrectable`, `reported_uncorrect`, `command_timeout`): accept what a drive already has, e.g. `{"WFG16T45": {"reallocated": 8}}`. The alarm returns, as `sdX: reallocated=9 (baseline 8)`, when the count grows past the number. Record why and when in your own notes; a growing count means the drive is failing |
| `journal_ignore` | list of regexes; an error-level journal line matching one (anywhere in the line, unit name included) is not counted. For noise particular to this box, e.g. `"NVRM: loading NVIDIA UNIX"`, which that driver logs at error level on every boot. An invalid pattern is skipped with an `info` note |
| `smart_timeout_s` | seconds each drive's `smartctl` may take in the 15-min run (default 60). Drives are read one at a time and the run has a 20-min limit: 180 + 300 + drives × this must stay under 1200 with room to spare, i.e. 11 drives at the default |
| `ipmi_timeout_s`, `gpu_timeout_s` | seconds each `ipmitool` / `nvidia-smi` call in the sensors run may take before it is killed and reported as an alarm (defaults 20 and 15). Three of each must fit inside `horatio-sensors.service`'s `TimeoutStartSec=2min`, or a hung BMC gets the run killed before its alarm is written |
| `fan_rpm_alarm_above` | alarm when any fan exceeds this RPM; `null` = off |
| `gpu_temp_alarm_margin_c` | alarm when a GPU's core or memory temperature is within this many °C of the driver's max operating temperature for it (including the relative GPU T.Limit reading on newer cards); absent = 3, `null` = off (the slowdown, ECC and retired-page GPU alarms stay on) |
| `backup_timer` | systemd timer whose last trigger is checked (alarm after 30 h); `null` = none. Give it `Persistent=true`: systemd forgets the last trigger of other timers at reboot, so until the timer next fires the snapshot only notes `has not fired since boot` (an alarm once the box has been up 30 h) |

A missing file uses built-in defaults (the shipped fault phrases, an empty defect baseline, no
fan ceiling, a 3 °C GPU temperature margin, no backup timer); malformed JSON is a `collector: load_config crashed` alarm, and the parts that
need the file crash too once they reach a value from it (SEL hardware faults at the first
hardware event line, each SAS drive as its own error row); everything else still reports.

## The rules live here, nowhere else

`alarms` in the two snapshots is the single rule set: the scripts decide what is a problem and
emit one string per finding; the dashboard's Hardware card (and any MOTD script) prints both
lists and adds no rules of its own. The card counts the 60 s file's alarms only while that file
is fresh; a stale or missing one shows as "sensors stale" (alarms unknown), never as clear.
`info` carries non-problems worth knowing (a drive in standby, a scrub in progress,
no self-test on record) and hardware that is not there to monitor: with no BMC,
`sel_memory` and `sel_hardware` are `null`; with no DIMMs under EDAC (non-ECC memory, or
ECC without its driver), `edac` is `null`. Each comes with one note, so an absent source
never shows as a zero count. Any capture that failed or timed out is itself an alarm
(`collector: …`), so a partial snapshot never reads as healthy; the 15-min run also alarms when
the 60 s file is missing or older than 5 minutes. A part of the 15-min run that crashes on a
value it cannot handle is a `collector: <part> crashed: …` alarm; the other parts still report,
and the run still exits 1. Nothing is mailed for an alarm: only a failed
15-min run mails root (its unit's `ExecStopPost=`); the 60 s unit never mails.

## Alarm identity: `alarm_items` and `checked`

The alarm strings carry counts and ages (`journal: 3 … latest: …`, `backup: last trigger 31h
ago`), so two snapshots cannot be compared by text. Each snapshot therefore also has
(`schema: 1`):

- `alarm_items`: one `{"key", "text"}` per alarm, in the same order as `alarms` and with the
  same text. The key is the alarm's identity and never contains a count or an age. Drives go
  by serial, because `sdX` letters move between boots; BMC sensors by name and entity. Three
  kinds of key do carry a device name, for want of anything better, and so start afresh if
  the letters move: a drive that gave no serial or no report at all (`smart/dev:<name>/…`)
  an md array's members (`md/<array>/member/<dev>/…`), and EDAC channels
  (`ecc/dimm/mcN/dimmN/…`). A fault counter that never falls on its own (reallocated sectors, grown defects, ECC
  counts) also has `"value"`, so a reader can report `8 -> 9`; temperatures and ages have none.
  A key raised twice in one run gets `#2`, `#3`.
- `checked`: the key prefixes this run really evaluated, with a clean capture. A prefix `P`
  covers key `K` when `K == P` or `K` starts with `P/`. **An alarm that is gone and whose
  prefix is not listed is unknown, not cleared**: a section that crashed, a tool that timed
  out, a drive left in standby or gone from `/dev`, a box whose BMC stopped answering, a rule
  that is not judged right now (`mismatch_cnt` during a resync, the correctable ECC count
  while an uncorrectable one shows) list nothing. A drive is listed rule by rule, for what
  its report really contained: one that answered with its health but not its attribute
  table is checked for `…/health`, not for `…/reallocated`. A successful command with
  an unavailable field is also unknown: BMC `ns` rows, GPU `N/A` counters, missing SMART
  attributes and unreadable sysfs counters do not count as healthy observations.
  Uncorrectable ECC and fan-ceiling alarms have individual device keys; their display
  totals do not establish whether an omitted device recovered.
- `alarm_hidden` (15-min snapshot): keys of alarms that exist but are not printed, which is
  the journal messages past the eight shown. Everything else under a checked prefix that is
  absent is gone; these are not.
- An aggregate alarm may include `requires`, the exact checks needed to establish its
  recovery. Corrected ECC keeps its summed threshold and names every DIMM's corrected
  counter; a DIMM that disappears cannot make the old aggregate healthy. The server
  retains those dependencies while the alarm is active. Explicitly checking the alarm's
  own key still permits deliberate disabling of a rule. Older aggregate alarms without
  dependencies remain held when the new collector cannot prove their recovery.
- `boot_id`: the kernel's boot id. Counters that reset at boot (ECC, failed units, GPU ECC
  since driver load) make their alarms disappear at a reboot; a reader that sees the id
  change knows why.

| key | alarm |
|---|---|
| `smart/<serial>/health`, `/selftest`, `/temperature`, `/grown_defects`, `/media_errors`, `/percentage_used`, `/reallocated`, `/pending`, `/offline_uncorrectable`, `/reported_uncorrect`, `/command_timeout` | per drive; `smart/dev:<name>/…` for a drive that gave no serial |
| `smart/dev:<name>/unreadable` | nothing readable came back, so there is no serial to go by |
| `smart-tools` | smartmontools not installed |
| `sel/memory`, `sel/hardware`, `sel/dates` | BMC event log |
| `ecc/uncorrectable`, `ecc/correctable` | EDAC counters since boot |
| `md/<array>/degraded`, `/array_state`, `/mismatch`, `/member/<dev>/state/<flag>`, `/member/<dev>/errors` | md arrays |
| `backup/never`, `backup/overdue` | the backup timer |
| `unit/<unit>` | a failed systemd unit |
| `journal/<8 hex>` | one error-level journal message: a hash of who said what, with digit runs (pids, counts) ignored |
| `journal-more` | more distinct messages than the 8 shown (their keys are in `alarm_hidden`) |
| `sensors-snapshot/missing`, `/stale` | the 60 s file, judged by the 15-min run |
| `bmc/<fan\|temp\|power>/<name>@<entity>` | a BMC sensor whose status is not ok |
| `fans/ceiling/<name>@<entity>` | `fan_rpm_alarm_above`; one key per fan, so a missing fan cannot be cleared by its neighbors |
| `gpu/<index>/temp/<memory\|core\|tlimit>`, `/slowdown`, `/ecc`, `/retired_pages` | per GPU, by nvidia-smi index (PCI bus order) |
| `capture/<name>`, `unparsed/<name>` | a capture that failed, a line that did not parse |
| `collector/<part>`, `smart/dev:<name>/crash` | a part of the 15-min run that crashed; the check of one drive that crashed |

## Since when: the dashboard server keeps the history

The collectors keep no state: a snapshot says what is wrong now. Since when each alarm has
been there, and when it went, is tracked by the dashboard server from `alarm_items` and
`checked`, for these two snapshots and for its own alarms alike, and written to its
`logs/events.jsonl` (README.md, "What is kept in `logs/`"). `GET /api/alarms` is all of it
in one list; `/api/storage-health` and `/api/sensors` serve each `alarm_items` entry with its
`since`.

`state.json` also has `boot`: `{id, kernel, previous: {clean, ended, kernel}}`. `previous` is
how the boot before this one ended, read from the journal: `clean` is true when its last lines
include journald's own `Journal stopped`, false after a power cut or a hang; `null` when the
journal holds only the current boot. An unclean end adds an `info` note for the first day,
and the server logs a `boot` event when `boot_id` changes.

| alarm starts with | first move |
|---|---|
| `SEL: N memory events in 7d` / `ECC: N uncorrectable\|correctable errors since boot` | `sudo ipmitool sel elist \| grep -i ecc` names the DIMM; `sudo ras-mc-ctl --errors` for the OS-time trend. Before any `ipmitool sel clear`: `ipmitool sel save <file>` — the SEL is the only record of POST-time errors |
| `SEL: N hardware events in 7d` | a PSU / fan / drive / thermal fault the BMC logged: `sudo ipmitool sel elist` (the lines are also in `sel_hardware`), then the matching sensor: `ipmitool sdr type 'Power Supply'` / `type fan`, or the drive. Tune `sel_fault_patterns` |
| `sdX: SMART health FAILED` / `sdX (serial): grown defects N > baseline M` | `sudo smartctl --all /dev/sdX`; identify by serial; plan the swap; set the new serial in `grown_defect_baseline` |
| `sdX: reallocated=N` / `reported_uncorrect=` / `command_timeout=` / `pending=` / `offline_uncorrectable=` (one line per attribute) / `sdX: NC, trip point` | `sudo smartctl --all /dev/sdX`; check your notes for this box's known-benign attributes first (some vendors pack operation counters into "error" attributes). When the count started is in `GET /api/alarms` and the server's `logs/events.jsonl`. A few remapped sectors with nothing pending can be accepted with `smart_baseline`; the alarm then returns only if the count grows |
| `sdX: unreadable (…)` | nothing readable came back: drive dropped, a USB stick, `timeout` killed smartctl, or SMART disabled: `lsblk`, `dmesg`, `sudo smartctl --all /dev/sdX` |
| `smartmontools not installed: drives are not monitored` | `apt install smartmontools`, then `sudo systemctl start horatio-collect.service` |
| `sdX: self-test log has errors` | a scheduled self-test failed: `sudo smartctl --log selftest /dev/sdX` |
| `nvmeXnY: media_errors=N` / `nvmeXnY: percentage_used=N (endurance spent)` | NVMe health log (`sudo smartctl --all /dev/nvmeXnY`): unrecoverable data errors, or the rated write endurance is used up. `passed` covers the rest of `critical_warning` (spare, temperature, read-only) |
| `mdN: DEGRADED` / `mdN: sdX state faulty` / `mdN: sdX errors=` / `mdN: array_state` | `cat /proc/mdstat`; `sudo mdadm --detail /dev/mdN` |
| `mdN: mismatch_cnt=N` | blocks differed between mirrors at the last `check` (not raised while a check/resync/repair runs: the count climbs during one); on RAID1 a small count can be benign. `echo repair > /sys/block/mdN/md/sync_action`, then a `check`; if it returns, look at the members' SMART |
| `fan <name>: N RPM over C RPM ceiling (boost back on, manual override, or box hot)` | only with `fan_rpm_alarm_above` set: a BMC fan setting reverted (power-cycle?), the fans are held at a manual speed above the ceiling, or the box is hot |
| `GPU N: memory X C >= max operating Y C - M` / `GPU N: core X C >= …` | within `gpu_temp_alarm_margin_c` of the driver's limit (`nvidia-smi -q -d TEMPERATURE`). The memory has no throttle of its own: cut the load or raise the fans. The core throttles itself at its max operating temp |
| `GPU N: T.Limit X C <= max operating Y C + M` | the driver's relative GPU thermal margin is within `gpu_temp_alarm_margin_c` of its max operating threshold. T.Limit counts downward as the GPU heats up; it is not an absolute core or memory temperature |
| `GPU N: thermal slowdown active (sw\|hw)` | the driver (sw, at max operating temp) or the board (hw, at slowdown temp) is cutting clocks for heat: `nvidia-smi -q -d TEMPERATURE,PERFORMANCE`. Power-cap throttling is normal and never alarms. `gpus[].sw_thermal_s` / `hw_thermal_s` count the seconds since driver load |
| `NVIDIA driver/library version mismatch: …` | observed directly by `nvidia-smi`; finish the driver update and reboot. The server may add installed/loaded version details, but the collector preserves the alarm even when those are unavailable |
| `GPU N: K uncorrected ECC errors since driver load` / `GPU N: retired pages pending` | `nvidia-smi -q -d ECC,PAGE_RETIREMENT`; pending pages retire at the next driver reload (reboot). Repeats mean the card is failing |
| `<sensor>: status <not ok>` | `sudo ipmitool sdr type fan` / `type temperature`: the BMC's own threshold crossed |
| `backup: <timer> has never fired` / `backup: last trigger Nh ago` / `unit failed: <backup service>` | `journalctl --unit <service>`; root's mail has the tail |
| `collector: backup-timer: no such timer: <timer>` | `backup_timer` in the config names a unit systemd does not know: fix the name |
| `unit failed: X` | `systemctl status X` |
| `journal: <unit>: <message> (N in 24h, …)` / `journal: K more distinct error-level messages in 24h` | one line per distinct error-level message of the last 24 h, oldest first, at most 8: `sudo journalctl --priority err --since -24hours`; `journal_errors.distinct` has every text. Each stays for 24 h after its last occurrence. Noise particular to this box goes in `journal_ignore`; noise every box has is `JOURNAL_IGNORE` in `health-collect.sh` |
| `collector: N SEL lines with unrecognised date format` | ipmitool's date format changed (locale or version): fix `SEL_DATE` in `health-collect.sh` |
| `collector: <cmd>: …` | that command failed inside a collector: run it by hand as root |
| `collector: <part> crashed: …` | a bug or an input the 15-min run cannot read; that part's section is missing from the snapshot (for `smart crashed: sdX`, only that drive: it shows as an error row and the other drives are still checked). Traceback: `journalctl --unit horatio-collect.service` |
| `collector: sensors snapshot Ns old` / `no sensors snapshot` | `systemctl status horatio-sensors.timer horatio-sensors.service` |
| `sensors stale` (dashboard) | the 60 s file is over 5 minutes old or unreadable, so the fan / GPU alarms are unknown: same as above |
| `stale` (dashboard) | `systemctl status horatio-collect.timer`; `sudo systemctl start horatio-collect.service` |

## Testing without hardware

Run `uv run --locked pytest tests/collector` from the repository root for GPU capture and alarm
fixtures. These run the script's actual GPU shell block with a fake `nvidia-smi`, then its
actual Python parser, without root or hardware. Older drivers that reject thermal-counter
query fields are retried without those counters; the other GPU readings and alarms remain available.

The python of each script runs standalone against a directory of hand-written capture files,
with `HEALTH_SYSBLOCK` pointed at a fake sysfs tree. Every file the bash stage would have written
must exist (a missing one crashes the part that reads it), so start from the `touch` line.
`tests/collector/test_collect.py` does exactly this for every part:

```bash
mkdir -p /tmp/stage /tmp/fake-sysblock
touch /tmp/stage/{sel,lsblk,failed,backup-timer,journal-err}.err /tmp/stage/{sel,failed,backup-timer,journal-err}.txt /tmp/stage/lsblk.json /tmp/stage/sensors.json
echo system-backup.timer > /tmp/stage/backup-timer.name   # the timer the bash stage chose; empty = none
# (sensors.json next to out.json: the full run alarms when it is missing or >5 min old)
# then hand-write sel.txt, lsblk.json (`lsblk --json --output NAME,TYPE,MOUNTPOINTS`, the tree), failed.txt,
# backup-timer.txt (epoch seconds or empty), smart_sdX.json/.rc/.err as needed.
# Extract the python BODY (the heredoc line reads shell vars a bare `bash` run would not set)
# and run it with the staging dir and output path as argv:
sed --quiet '/^python3 - /,/^PY$/p' health-collect.sh | sed '1d;$d' > /tmp/collector.py
HEALTH_SYSBLOCK=/tmp/fake-sysblock HEALTH_CONFIG=./health-config.example.json python3 /tmp/collector.py /tmp/stage /tmp/stage/out.json
```

The ECC rules read `HEALTH_EDAC` (a fake `mc0/dimm0/{dimm_label,dimm_ce_count,dimm_ue_count}`
tree; without the override the test machine's own, usually empty, EDAC is read) and `HEALTH_UPTIME`
(a file in `/proc/uptime` format); the md rules read `HEALTH_SYSBLOCK` (`md0/md/{level,degraded,
sync_action,array_state,mismatch_cnt}` plus `md0/md/dev-sdX/{state,errors}`).

`health-sensors.sh` is tested the same way with its own stage: `touch` `{fan,temp,power,rapl,gpu,gpu-temp}.err`
and `{fan,temp,power,rapl1,rapl2,gpu,gpu-temp}.txt`, hand-write the three `ipmitool sdr type …` tables (five
`|`-separated columns per line) and the two RAPL files (first line: a fractional epoch; then
`<path> <name> <energy_uj> <max_energy_range_uj>` per domain, the second file one second and some
microjoules later), extract its python body the same way and run it with `stage out.json`.
Empty `gpu.txt` / `gpu-temp.txt` is a box without nvidia-smi. On a GPU box, capture them live
(`nvidia-smi` needs no root) with the two commands in the script's bash stage (the `--query-gpu`
line into `gpu.txt`, `nvidia-smi -q -d TEMPERATURE` into `gpu-temp.txt`), then edit `gpu.txt` for
the hot cases: e.g. set a V100's memory column to 86 and its sw_thermal column to `Active` for
exactly `GPU 1: memory 86 C >= max operating 85 C - 3` and `GPU 1: thermal slowdown active (sw)`.

To exercise the hardware-fault filter, point `HEALTH_CONFIG` at a test config and stage a
`sel.txt` mixing a recent fault (`… | Power Supply #0x63 | Power Supply AC lost | Asserted` dated
within 7 days), a routine line (`Presence detected`), a recovery (`Fully Redundant`), and a
>7-day-old fault: exactly one `SEL: N hardware events in 7d` alarm, counting the recent matches.

On a live box, start a run with `sudo systemctl start horatio-collect.service`, not by
running the script directly: the unit serialises runs.
