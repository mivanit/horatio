# Operations

[Back to README](../README.md) · [Configuration](configuration.md) · [API](api.md)

These instructions cover the Python backend unless stated otherwise. For Lua on
Linux or OpenWrt, see the [Lua guide](../lua/README.md). Commands run from the
checkout root.

- [Installation options](#installation-options) and [optional tools](#optional-tools)
- [Process permissions](#process-permissions)
- [Updates](#updates) and [uninstall](#uninstall)
- [Access and security](#access-and-security)
- [Terminal](#terminal)
- [Logs and history](#logs-and-history), [alarms](#alarms), and [merging history](#merging-history)

## Installation options

```bash
git clone https://github.com/mivanit/horatio ~/horatio
cd ~/horatio
./install.sh               # web service on :8080; run as your user, it calls sudo where needed
./install.sh --collector   # the same, plus the root hardware collector
```

The installer offers to install nethogs and sysstat until they are installed (`--no-optional` silences the offers);
it grants them no extra permissions unless asked (see [Process permissions](#process-permissions)).
Then edit `config.json` (created from `config.example.json`: title, ping targets, thresholds)
and `sudo systemctl restart horatio`; with the collector, also the collector's config at
`/etc/horatio/config.json` (see the collector README; it takes effect on the next 15-min run).
Open `http://<host>:8080`, or from a terminal `curl http://<host>:8080/text` (`/help` lists the options and API). For a live terminal view, see [Terminal](#terminal). The checkout can live anywhere writable by the user who runs it (no
whitespace or `: % \ | &` in the path); the service file is rendered from `horatio.service` with that path and user.

After install, `uv run python python/server.py --set-password` sets the password for the raw history
download, settings changes, and alarm retirement (stored hashed in `.password`).

## Optional tools

- Linux with systemd; [uv](https://docs.astral.sh/uv/). The optional-tool offers use
  `apt-get`; on other distros install `nethogs` + `libcap2-bin` and `sysstat` by hand.
- Optional, per card:

| Card | Needs |
|---|---|
| GPUs | an NVIDIA driver (`nvidia-smi`) |
| Network Processes | `nethogs`, plus `--grant-capabilities` (see [Process permissions](#process-permissions)) |
| Disk I/O Processes | `sysstat` (`pidstat`); with `--grant-capabilities` it shows every process, not just the service user's |
| System Mail | a local MTA delivering root's mail to the service user's spool: `postfix` ("Local only") plus `bsd-mailx`, with `root: <user>` in `/etc/aliases` and `newaliases` |
| Temperatures, Fan Speeds, Power | nothing extra on most boxes: the dashboard reads Linux hwmon itself (`hwmon.py`; no root, no collector): every chip's temperatures, fans and power, e.g. an AMD CPU's power estimate or an ACPI power meter. A desktop board's fan chip often needs its Super I/O driver loaded first (`sudo sensors-detect`, then the module it names in `/etc/modules-load.d/`); the Hardware card says so when a desktop has no fan readings. The root collector adds what needs root (below) |
| Hardware health, Drive Health, BMC fans/temps/watts, CPU/DRAM watts | the root collector (`--collector`): `smartmontools` (required: without it the Hardware card alarms); optionally `ipmitool` + a BMC for fans/temps/system watts, Intel RAPL for CPU/DRAM watts and `nvidia-smi` for GPU memory temperatures and thermal alarms, skipped when absent. See [`collector/README.md`](../collector/README.md) |

## Process permissions

Seeing other users' processes needs root-level access that the dashboard does not have:
nethogs must capture packets and map sockets to processes, and pidstat must read each
process's I/O counters (`/proc/<pid>/io`, readable only by its owner). By default the
installer grants neither, so the Network Processes card shows an error and the Disk I/O
Processes card shows only the service user's processes.

`./install.sh --grant-capabilities` attaches Linux file capabilities to the two binaries
(`nethogs`: `cap_net_admin,cap_net_raw,cap_dac_read_search,cap_sys_ptrace`; `pidstat`:
`cap_dac_read_search,cap_sys_ptrace`). The service then sees every process. The tradeoff:
capabilities belong to the binary, not the service, so **every local user** who runs
`nethogs` or `pidstat` gets them. Anyone with a login on the box can see every process's
network and disk activity, and a bug in either tool could let them read any file
(`cap_dac_read_search`). Nothing is exposed over the network. Reasonable when you are the
only one with a login on the box; skip it otherwise.

`./install.sh --uninstall` removes them; by hand, `sudo setcap -r "$(command -v nethogs)"`
(likewise `pidstat`). Installs from before this flag existed gave nethogs its capabilities when you
accepted the nethogs offer; those stay until removed.

## Updates

```bash
./install.sh --update            # to the newest release; see Versions and updates
```

On a checkout that follows main (or one from before `--update` existed, once), the same is
`git pull && ./install.sh`. Either way the install is idempotent: re-syncs the venv, fetches Chart.js if it is missing or fails its checksum, re-renders the unit, restarts the service,
and re-installs the collector when it is installed on this box (its first run is synchronous,
a minute or three on a box with many drives). The optional-tool offers repeat until the tool
is installed, or pass `--no-optional`. Pass `--grant-capabilities` on updates only if you want to
(re)apply the process permissions; a package upgrade of nethogs or sysstat drops them. `config.json`, `/etc/horatio/config.json`, `.password`
and `logs/` are never touched.

### Versions and updates

Releases are `vX.Y.Z` git tags, with notes in [`CHANGELOG.md`](../CHANGELOG.md). `VERSION`
holds the number and changes only in a release, so `main`'s `VERSION` is the newest one.
The page footer shows the running version, and `GET /api/version` gives `{version, build}`
(`build`: the commit `install.sh` installed, from `git describe`, or null).

`./install.sh --update` fetches tags, checks out the newest `vX.Y.Z` and runs that
version's installer; flags such as `--collector` pass through. It refuses when tracked files
have local changes, or when the checkout is not behind that release (a checkout on main
past it). `./install.sh --update v1.2.0` goes to the version named, older ones included. It
prints how to go back. A release checkout is a detached HEAD, so plain `git pull` fails
there; use `--update`.

For release creation, see [Development](development.md#releases).

## Uninstall

```bash
./install.sh --uninstall
```

Removes the service, the collector's units, scripts and snapshots, and the file
capabilities on `nethogs` and `pidstat` (see [Process permissions](#process-permissions)). The checkout, its
`config.json`, `logs/` and `/etc/horatio/config.json` stay; delete them by hand.

## Access and security

Use a trusted LAN, not public interfaces. The Python dashboard binds to all
interfaces and is unauthenticated except for the raw history download, settings
changes, and alarm retirement. Unauthenticated data includes local IP
addresses, mail subjects, drive models and serials, and the Glances metrics; the Glances
proxy forwards only the plugins the UI reads (cpu, percpu, mem, memswap, gpu, network, diskio,
load, uptime, system, fs; sensors come from the server's own hwmon reader), so Glances' process views with full command lines are
unreachable, and the dashboard's own process list strips arguments. The web process
runs as an unprivileged user and never runs `sudo`, `smartctl` or `ipmitool`: the collector
runs as root on its own timers and the web process only reads the JSON it writes. The unit
sandboxes it: everything is read-only except the checkout's `logs/` (`ProtectSystem=strict`),
and `/home`, `/root` and `/run/user` are empty (`ProtectHome=tmpfs`) except the checkout and the
venv's Python, bound back in read-only. The checkout itself is read-only to the service because
root runs code from it (`collector/install.sh` copies the collector scripts, `install.sh`), and
`.password` is written only by `python/server.py --set-password`, run by hand. The IP card and each history record ask `api.ipify.org` for the public address.

Process labels retain recognized script paths or Python module names, but omit inline code
and unrecognized interpreter options. This protects live data and new history records;
upgrading does not scrub previously stored process names, which remain visible through the
process-history API when those records are in range.

The password does not protect the dashboard or ordinary JSON endpoints. Set it
with `uv run python python/server.py --set-password`; it is stored hashed in
`.password`. See [API authentication](api.md#authentication) for scripts.
`config.json` is served to visitors: do not put passwords or tokens in it.

## Terminal

Two ways to view a box from a terminal, both without a browser:

**One-off report.** Prints the current snapshot and recent history, then exits:

```bash
curl "http://<host>:8080/text?width=$COLUMNS"     # colour, unicode
curl http://<host>:8080/text?style=simple         # plain ASCII, for scripts and agents
```

**Live view (`python/tui.py`).** Fills the terminal and redraws every 2 s. Keys are listed at the bottom (q quits, v switches view, t table, +/- history window). It detects terminal size, colour depth and UTF-8; `--color` and `--style` override. Standard library only, Python 3.8+, Linux or macOS.

- **From anywhere, nothing installed.** The box serves the script, and the served copy already knows the box's address:
  ```bash
  curl -fsS http://<host>:8080/tui.py | python3 -
  ```
  This runs whatever the server sends, which is fine on your own network. Otherwise save it, read it, then run it: `curl -fsS -o tui.py http://<host>:8080/tui.py && python3 tui.py`.
- **From a horatio checkout.** `python3 python/tui.py` on the box itself connects to `http://localhost:8080`. Elsewhere, `python3 python/tui.py http://<host>:8080`, or set `HORATIO_URL`.

`http://<host>:8080/help` has the full list of options, with the box's real address filled in.

## Logs and history

Everything the dashboard remembers is in the checkout's `logs/` (the only directory its
service may write), as JSON lines. Measurements and events are separate files because they
are different things: one is a bulky sample on a schedule, the other a rare, small fact.

| file | one line per | written |
|---|---|---|
| `history.jsonl` | sample of the machine: CPU, memory, temperatures, fans, power, drives, GPUs, ping, processes | every `history.record_interval` |
| `services.jsonl` | round of `http_targets` checks | every `services.check_interval` |
| `events.jsonl` | thing that happened: an alarm `raised`, `changed` in value, `cleared`, `retired` or `expired`, a `boot`, a `gap` | when it happens; nothing while an alarm just stays |
| `alarms-state.json` | (not a log) what the alarm history currently holds, so a restart continues it | in place, about once a minute |
| `server.log`, `glances.log` | the server's own messages | rotated at 5 MiB |

The Python backend does not rotate these JSON-lines files. The Lua backend
[rotates history into gzip archives](../lua/README.md#openwrt). `/var/lib/horatio/` holds only the collectors'
two current snapshots, not history.

## Alarms

`events.jsonl` covers hardware and host alarms: the two root collectors' (by the `key`
each gives in `alarm_items`) and the server's own fan rules and host checks. Each line has `timestamp`,
`source` (`state`, `sensors` or `dashboard`) and `event`:

| `event` | when | other fields |
|---|---|---|
| `raised` | an alarm appears | `key`, `text`, `value`, `since`; `initial: true` when it was already there the first time its source was seen |
| `changed` | its `value` differs from the last snapshot (reallocated sectors `8 -> 9`) | the same, plus `was` |
| `cleared` | it is gone, and the snapshot says it `checked` for it | the same, plus `active_for_s`; `after_boot: true` when the box rebooted in between (counters that reset at boot) |
| `expired` | it could not be re-checked for 30 days and at least 3 snapshots (a drive that is gone) | the same, plus `unchecked_since` |
| `retired` | an authenticated user explicitly retires a held alarm | the same, plus `unchecked_since`; this records retirement, not proof of recovery |
| `boot` | the 15-min snapshot shows a new boot | `id`, `kernel`, `previous: {clean, ended, kernel}` |
| `gap` | a source was not seen for more than three of its intervals (box off, timer stopped), or the server itself was not running | `from`, `to` |

An alarm that is absent only because it could not be looked at (a failing drive that is
asleep, a BMC that timed out, a fan that stopped answering) is kept, not cleared; the
Hardware card shows it as "not re-checked". The 60 s snapshot's alarms and the fan rules
must be absent three times in a row to clear, so a value sitting on its threshold does not
write two lines a minute. While the server is not running nothing is recorded: an alarm that
both came and went in that time is missed, and the next line is a `gap` with
`source: "dashboard"`. A `gap` on `state` or `sensors` means that collector's snapshots
stopped while the server was running. Service outages are not repeated here;
`services.jsonl` has every check.

After deliberately removing or replacing hardware, use **Retire** beside its
held alarm on the Hardware card. It requires the dashboard password set with
`uv run python python/server.py --set-password`. Retirement removes that occurrence
from current concerns and preserves its event history; a new observation of the
fault raises a new alarm. Active alarms cannot be retired. If the alarm changes
while the dialog is open, refresh and review it again. This also handles obsolete
label/index alarm keys from upgrades without guessing which current device they
belonged to.

Held alarms also expire after 30 days without a check and at least three
unchecked snapshots. Expiry records missing evidence, not recovery.

Each line's `timestamp` is when the thing was observed: for a collector's alarm, the time of
the snapshot that showed it, which can be up to 30 s before the line was written. So lines
from different sources are close to, but not strictly in, time order. A pass is written in
one piece; the one way to see a line twice is the server dying between writing it and
saving `alarms-state.json`.

```bash
tail logs/events.jsonl                                   # what changed lately
curl -s localhost:8080/api/alarms | python3 -m json.tool # what is wrong now, since when
```

### Dashboard alarm sources

The dashboard's own source (`dashboard`) covers fans below their configured
`min_rpm`, setup hints (a desktop with no fan readings, or a VM), and host checks:

- `/run/reboot-required` is a note until it is a week old, then an alarm. Its age
  is the file's birth time.
- An NVIDIA kernel module that differs from the installed library alarms at
  once: `nvidia-smi` and GPU readings fail until a reboot. The message says
  whether rebooting into the same kernel fixes it.
- ZFS alarms (`zfs/` keys, from `zfs.py`) cover a pool not ONLINE, device or data
  errors, a pool never scrubbed, a scrub older than `zfs_scrub_max_days`, an
  expected pool missing, unhealthy spare disks, or unreadable state while pools
  are expected. Spare state is checked separately from I/O counters; a missing
  spare retains its prior alarm until explicitly retired or observed healthy.
  A scrub in progress is a note.

See [sensor and ZFS configuration](configuration.md) for rules, the
[collector guide](../collector/README.md) for hardware troubleshooting and raw
snapshot contracts, and the [alarms API](api.md#hardware-and-alarms) for response fields.

## Merging history

For moving to a new box, combining a router's and a server's, or folding a
router's gzipped archives back into one file: `scripts/history-merge.sh -o merged.jsonl
logs/ other-box-logs/ some.jsonl.gz` reads `logs/` directories or single files, sorts by
timestamp, and drops only byte-identical lines. It never changes its inputs or overwrites
`-o`. To use the result, stop the dashboard, keep the old `logs/history.jsonl`, and put the
merged file in its place. It runs on BusyBox too, but holds the data in RAM there, so merge
large histories on a Linux box.
