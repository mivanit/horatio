Dashboard/logging of basic system info (cpu/ram/temps/disk io/network/etc) for homelabs.

Backend: FastAPI wrapping a [Glances](https://github.com/nicolargo/glances) subprocess plus
`nvidia-smi`, `ping`, `nethogs`, `pidstat`. Frontend: vanilla JS and Chart.js, no build step.

[Live demo](https://mivanit.github.io/horatio/): the real frontend with synthetic data (see [`docs/`](docs/README.md)).

# Requirements

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
| Hardware health, Drive Health, BMC fans/temps/watts, CPU/DRAM watts | the root collector (`--collector`): `smartmontools` (required: without it the Hardware card alarms); optionally `ipmitool` + a BMC for fans/temps/system watts, Intel RAPL for CPU/DRAM watts and `nvidia-smi` for GPU memory temperatures and thermal alarms, skipped when absent. See [`collector/README.md`](collector/README.md) |

## Install

```bash
git clone https://github.com/mivanit/horatio ~/horatio
cd ~/horatio
./install.sh               # web service on :8080; run as your user, it calls sudo where needed
./install.sh --collector   # the same, plus the root hardware collector
```

It offers to install nethogs and sysstat until they are installed (`--no-optional` silences the offers);
it grants them no extra permissions unless asked (see [Process permissions](#process-permissions)).
Then edit `config.json` (created from `config.example.json`: title, ping targets, thresholds)
and `sudo systemctl restart horatio`; with the collector, also the collector's config at
`/etc/horatio/config.json` (see the collector README; it takes effect on the next 15-min run).
Open `http://<host>:8080`, or from a terminal `curl http://<host>:8080/text` (`/help` lists the options and API). For a live terminal view, see [Terminal](#terminal). The checkout can live anywhere writable by the user who runs it (no
whitespace or `: % \ | &` in the path); the service file is rendered from `horatio.service` with that path and user.

After install, `uv run python server.py --set-password` sets the password for the raw history
download and the settings endpoint (stored hashed in `.password`).

### Process permissions

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

### Update

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

## Versions and updates

Releases are `vX.Y.Z` git tags, with notes in [`CHANGELOG.md`](CHANGELOG.md). `VERSION`
holds the number and changes only in a release, so `main`'s `VERSION` is the newest one.
The page footer shows the running version, and `GET /api/version` gives `{version, build}`
(`build`: the commit `install.sh` installed, from `git describe`, or null).

`./install.sh --update` fetches tags, checks out the newest `vX.Y.Z` and runs that
version's installer; flags such as `--collector` pass through. It refuses when tracked files
have local changes, or when the checkout is not behind that release (a checkout on main
past it). `./install.sh --update v1.2.0` goes to the version named, older ones included. It
prints how to go back. A release checkout is a detached HEAD, so plain `git pull` fails
there; use `--update`.

To cut a release: `make release V=X.Y.Z` (writes `VERSION`, dates the changelog's
Unreleased section, commits and tags), then `git push origin main vX.Y.Z`.

## Uninstall

```bash
./install.sh --uninstall
```

Removes the service, the collector's units, scripts and snapshots, and the file
capabilities on `nethogs` and `pidstat` (see [Process permissions](#process-permissions)). The checkout, its
`config.json`, `logs/` and `/etc/horatio/config.json` stay; delete them by hand.

## Terminal

Two ways to view a box from a terminal, both without a browser:

**One-off report.** Prints the current snapshot and recent history, then exits:

```bash
curl "http://<host>:8080/text?width=$COLUMNS"     # colour, unicode
curl http://<host>:8080/text?style=simple         # plain ASCII, for scripts and agents
```

**Live view (`tui.py`).** Fills the terminal and redraws every 2 s. Keys are listed at the bottom (q quits, v switches view, t table, +/- history window). It detects terminal size, colour depth and UTF-8; `--color` and `--style` override. Standard library only, Python 3.8+, Linux or macOS.

- **From anywhere, nothing installed.** The box serves the script, and the served copy already knows the box's address:
  ```bash
  curl -fsS http://<host>:8080/tui.py | python3 -
  ```
  This runs whatever the server sends, which is fine on your own network. Otherwise save it, read it, then run it: `curl -fsS -o tui.py http://<host>:8080/tui.py && python3 tui.py`.
- **From a horatio checkout.** `python3 tui.py` on the box itself connects to `http://localhost:8080`. Elsewhere, `python3 tui.py http://<host>:8080`, or set `HORATIO_URL`.

`http://<host>:8080/help` has the full list of options, with the box's real address filled in.

## Configuration

`config.json` (per machine, gitignored):

| key | meaning |
|---|---|
| `title`, `title_emoji` | page title and heading |
| `ping_targets` | `[{address}]` pinged every poll and recorded to history |
| `http_targets` | Linux server only. `[{name, url}]`: URLs requested every `services.check_interval` seconds, for the Services card and its outage history. A target is up when the request, after redirects, returns a status below 400; `"status": 401` requires that exact status instead, and `"insecure": true` skips the TLS certificate check (self-signed certificates). A failed request is repeated once after 2 s before it counts as down. Absent or `[]` turns the feature off and hides the card (the default). **`config.json` is served to every visitor of the dashboard, so put no passwords or tokens in a URL.** To watch another Horatio, use its `/api/version`. A Horatio that is down checks nothing, so its own downtime shows as "no checks", not as an outage: have two boxes watch each other |
| `services.check_interval`, `services.timeout` | seconds between rounds of `http_targets` checks (default 60, 10 to 3600) and the longest one request may take in total (default 5, 1 to 30). Each round appends one line to `logs/services.jsonl`, which is never rotated or deleted: about 0.1 MB a day per target at 60 s |
| `history.record_interval` | seconds between history records (default 600) |
| `history.min_record_interval` | minimum seconds between manual "record now" requests, 0 to disable (default 60) |
| `history.sample_duration`, `history.sample_count` | each record averages `sample_count` samples over `sample_duration` seconds |
| `history.max_file_mb` | OpenWrt backend only: past this size the older part of `logs/history.jsonl` is gzipped beside it and the file keeps its newest half (default 16, some months of records). Rotation runs in a background process on the router, so a large file cannot time out a request; it needs free space for the compressed archive and the retained half, and never deletes history (failures are logged to syslog and retried after the next record). History requests read at most the newest 4 MiB and report `truncated` when the requested range does not fit |
| `history.max_hours` | Linux server only (OpenWrt reads at most its 4 MiB tail anyway): largest window, in hours, any history read serves (`/api/history`, `/api/history/field/<name>`, `/text`); larger requests are clamped to it. Lower it on a small box: a year-long window parses up to the 64 MB history tail into memory (default 8760, 0.1 to 8760) |
| `history.max_gap_seconds` | history charts leave a stretch empty when two records are further apart than this (the box or the recorder was off), instead of joining them with a line; default 3600, `0` joins everything |
| `history.processes_enabled`, `history.process_count` | log the top `process_count` processes by CPU and by memory, plus any holding GPU memory, in each history record (`processes`: pid, name always; ppid, create_time, user, status, threads, cpu_percent, rss_mb, disk_read_kbps, disk_write_kbps, gpu_mem_mib + gpus, gpu_util_percent when known and non-zero); default off, 10. Roughly doubles a record's size at 10, so the 64 MB history tail covers fewer days |
| `thresholds.*_percent`, `temp_celsius`, `network_mbps`, `disk_io_mbps`, `ping_ms` | orange-card thresholds |
| `thresholds.power_psu_watts`, `power_max_watts`, `power_warn_fraction` | the power bar's dashed PSU line (absent: no line and no PSU warning), its right edge (absent: the PSU plus 10 %, or without a PSU 1.5 times the reading rounded up to 50 W), and the orange threshold as a fraction of the PSU |
| `sensor_labels` | display names for any sensor (temperature, fan, power), e.g. `{"loc1": "I350 NIC", "f71889ed fan1": "CPU fan"}`, used by the cards and their history charts. History records keep the sensor's own name, so renaming never splits a series. `temp_labels` is the older name and still read; `sensor_labels` wins where both name a sensor |
| `fans` | rules per fan, by the sensor's own name (e.g. `f71889ed fan1`) or its `sensor_labels` name, plus `default` for every fan not named: `{"f71889ed fan1": {"min_rpm": 300}, "default": {"hide_if_zero": true}}`. Below `min_rpm` is an alarm on the Hardware card (`"on_low": "note"` makes it a note) and the fan always stays visible. `hide_if_zero` (default true) hides a fan reading 0 that has no minimum and has not been seen turning since the server started: an empty header is not a stopped fan, but a fan that stops is shown at 0 and recorded. A rule with `min_rpm` naming a fan that cannot be read is an alarm; any other rule naming a missing fan, and a `min_rpm` that is not a number, is a note, so a typo cannot silently disable an alarm. Alarms show the `sensor_labels` name. The server reads it at startup |
| `temp_thresholds` | a limit in °C for single sensors that run hot by design, e.g. `{"I350 NIC": 95}` (the sensor's own name, or its `sensor_labels` / `temp_labels` name; the card's short forms "Pkg", "NVMe" and "Cores" are not names). On the card that sensor turns orange at its limit instead of at its source's or `thresholds.temp_celsius`. In `/text` the Temperatures line's `max` is coloured by the sensor nearest its own limit, and a GPU row's temperature by `GPU N`'s limit; the history `MaxT` row still uses `thresholds.temp_celsius` |
| `limits.network_mbps`, `limits.disk_io_mbps` | ceilings (MB/s) for each network interface and each drive, because Glances' rates occasionally come out absurd (default 1250, i.e. 10 Gbit/s, and 15000). A sample over the cap counts as the cap, so a spike is shortened, not removed. Totals are the sums of the capped interfaces / drives and are not capped themselves (bridges, veths and tunnels count the same traffic more than once). Applies to the live cards, history and `/text`, not to the raw `/api/4/*` endpoints or to history on the OpenWrt backend. A record that hit a cap lists it under `rate_capped` (`metric`, `interface` or `device` + `drive`, `samples` of `of`, `raw_max`, `cap`) and the server logs it; each record notes the caps it was taken under in `rate_caps`. The server reads the limits at startup (restart after changing them); the page reads them on load. Records without `rate_caps` (recorded before the caps) are clamped when read; records with it are served as recorded, so a changed cap applies to new records only |

Runtime settings (poll interval, chart length, grid columns, card order, theme) live in the browser.

**Themes**: the toolbar's Theme picker lists the CSS files in `themes/`. `light`, `classic`,
`high-contrast`, `print` (also for e-ink) and `phosphor` ship with Horatio, and any other `themes/<name>.css` you add appears too. A theme overrides the
color, font and shape variables in `static/style.css` and can restyle any selector. See
[themes/README.md](themes/README.md) for how to write one (it is written so you can hand it to
an agent along with a description of the look you want).
History recording settings can also be changed from the page with the password; they revert to
`config.json` on restart.

**Merging history** (moving to a new box, combining a router's and a server's, or folding a
router's gzipped archives back into one file): `scripts/history-merge.sh -o merged.jsonl
logs/ other-box-logs/ some.jsonl.gz` reads `logs/` directories or single files, sorts by
timestamp, and drops only byte-identical lines. It never changes its inputs or overwrites
`-o`. To use the result, stop the dashboard, keep the old `logs/history.jsonl`, and put the
merged file in its place. It runs on BusyBox too, but holds the data in RAM there, so merge
large histories on a Linux box.

## What is kept in `logs/`

Everything the dashboard remembers is in the checkout's `logs/` (the only directory its
service may write), as JSON lines. Measurements and events are separate files because they
are different things: one is a bulky sample on a schedule, the other a rare, small fact.

| file | one line per | written |
|---|---|---|
| `history.jsonl` | sample of the machine: CPU, memory, temperatures, fans, power, drives, GPUs, ping, processes | every `history.record_interval` |
| `services.jsonl` | round of `http_targets` checks | every `services.check_interval` |
| `events.jsonl` | thing that happened: an alarm `raised`, `changed` in value, `cleared` or `expired`, a `boot`, a `gap` | when it happens; nothing while an alarm just stays |
| `alarms-state.json` | (not a log) what the alarm history currently holds, so a restart continues it | in place, about once a minute |
| `server.log`, `glances.log` | the server's own messages | rotated at 5 MiB |

None of the JSON-lines files is rotated. `/var/lib/horatio/` holds only the collectors'
two current snapshots, not history.

`events.jsonl` covers every alarm, whoever decides it: the two root collectors' (by the `key`
each gives in `alarm_items`) and the server's own fan rules. Each line has `timestamp`,
`source` (`state`, `sensors` or `dashboard`) and `event`:

| `event` | when | other fields |
|---|---|---|
| `raised` | an alarm appears | `key`, `text`, `value`, `since`; `initial: true` when it was already there the first time its source was seen |
| `changed` | its `value` differs from the last snapshot (reallocated sectors `8 -> 9`) | the same, plus `was` |
| `cleared` | it is gone, and the snapshot says it `checked` for it | the same, plus `active_for_s`; `after_boot: true` when the box rebooted in between (counters that reset at boot) |
| `expired` | it could not be re-checked for 30 days (a drive that is gone) | the same, plus `unchecked_since` |
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

Each line's `timestamp` is when the thing was observed: for a collector's alarm, the time of
the snapshot that showed it, which can be up to 30 s before the line was written. So lines
from different sources are close to, but not strictly in, time order. A pass is written in
one piece; the one way to see a line twice is the server dying between writing it and
saving `alarms-state.json`.

```bash
tail logs/events.jsonl                                   # what changed lately
curl -s localhost:8080/api/alarms | python3 -m json.tool # what is wrong now, since when
```

## Security

Meant for a trusted LAN, not public interfaces. probably has many security holes. The dashboard binds to all interfaces and is unauthenticated except
the raw history download and the settings endpoint. Unauthenticated data includes local IP
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
`.password` is written only by `server.py --set-password`, run by hand. The IP card and each history record ask `api.ipify.org` for the public address.

Process labels retain recognized script paths or Python module names, but omit inline code
and unrecognized interpreter options. This protects live data and new history records;
upgrading does not scrub previously stored process names, which remain visible through the
process-history API when those records are in range.

again, don't put this on anything besides a trusted LAN!

## API

| Endpoint                     | Description                                 |
| ---------------------------- | ------------------------------------------- |
| `GET /`                      | Dashboard UI                                |
| `/api/4/<plugin>`            | Proxied to the Glances REST API for the plugins the UI reads (cpu, percpu, mem, memswap, gpu, network, diskio, load, uptime, system, fs); anything else is 404 |
| `GET /api/4/sensors`         | Served by the dashboard from Linux hwmon, in Glances' sensors shape (`type` `temperature_core` / `fan_speed` / `power`, `label`, `value`, `unit`, `warning`, `critical`; power rows also `total`, true only for a whole-machine reading). Temperature labels are Glances' own, so history continues; fans are `<chip> fan<N>` (or the kernel's label), with the `fans` rules applied |
| `GET /api/sensor-alarms`     | `{alarms, notes}` the dashboard itself decides: fans below their `min_rpm`, setup hints (a desktop with no fan readings, a VM). The collectors' alarms stay in their own files; `GET /api/alarms` has all of them in one list |
| `GET /api/4/fs`              | Glances filesystems, deduplicated by device (bind mounts collapse) |
| `GET /api/processlist`       | Process list with arguments stripped (the one the UI uses) |
| `GET /api/list-endpoints`    | Which file-backed endpoints exist on this box (mail, storage-health, sensors) |
| `GET /api/version`           | `{version, build}`: the running release and, on a git checkout, the commit `install.sh` installed (null otherwise) |
| `GET /api/gpu-info`          | NVIDIA GPU details                          |
| `GET /api/cpu-info`          | CPU details from /proc/cpuinfo              |
| `GET /api/gpu-processes`     | GPU process list: per (GPU, pid) memory plus `gpu_util_percent` / `gpu_mem_util_percent` from `nvidia-smi pmon` (null when the driver had no sample for that pid, pmon is unavailable or slow, or the GPU is past pmon's four) |
| `GET /api/network-processes` | Per-process network bandwidth (nethogs)     |
| `GET /api/disk-processes`    | Per-process disk I/O (pidstat)              |
| `GET /api/ping`              | Ping the configured targets                 |
| `GET /api/ip-info`           | LAN, public, and Tailscale IP addresses     |
| `GET /api/mail`              | Local system mail: `count` and the newest 10 `{date_epoch, subject}`; always 200 with `status` ok/missing/error |
| `GET /api/storage-health`    | The 15-min collector snapshot (`alarms`, `info`, drives, md, ECC, SEL, backup, failed units, journal errors) with `status` ok/stale/missing/error and `age_sec`, plus the dashboard's own `sensor_alarms` / `sensor_notes` (as `/api/sensor-alarms`, whatever the collector's state); always 200. A source the box does not have (no BMC, no ECC reporting) is `null` with one `info` note |
| `GET /api/sensors`           | The root collector's 60 s snapshot (`fans`, `temps`, `power`, `gpus` with nvidia-smi temperatures, driver limits, thermal slowdown and ECC, `alarms`), same shape, stale after 300 s. Power rows carry `unit` and `total` (the BMC's whole-machine reading) |
| `GET /api/alarms`            | Everything wrong right now in one list, and since when. `active`: `{source, key, text, value, since, initial}` rows from the 15-min snapshot (`source: "state"`), the 60 s snapshot (`"sensors"`, only while it is fresh), the server's own fan rules (`"dashboard"`) and the service checks (`"services"`: a target that is down, `since` = its first failed check). `sources` gives each one's `status`: one that is not `ok` means unknown, never clear. `since` is epoch seconds, from the server's alarm history; `initial: true` means the alarm was already there when the history began, so the real start is unknown; `key` and `since` are `null` with a collector too old to give its alarms keys. `held`: alarms a collector could not re-check (a failing drive that is asleep). `recent`: the newest 50 lines of `logs/events.jsonl` (below) |
| `GET /api/services`          | Top-level `stale: true` when no check round has finished for several intervals (the results are then old; the card and `/text` show them as stale). Each `http_targets` entry: `ok` (null before its first check), `status`, `ms`, `error`, `checked`, `down_since` (epoch of the first failed check of the current outage, at most a week back; `null` when up), and `uptime` as `{up, checks}` counts over the last `1`, `24` and `168` hours. `checks: 0` means nothing was checked in that window |
| `GET /api/services/history`  | The outage grid (query: `hours`, default 24): per target, `count` pairs of `[passed, failed]` checks, one per `bucket_sec` seconds from `start` (at most 300 buckets). `[0, 0]` is a bucket with no checks. `truncated: true` means the log could not be read back as far as `start` |
| `GET /api/history`           | History records (query: `hours`, default 24) without the heavy fields (`temps`, `processes`) and `ip`. Each record also carries `cpu_freq` (min/median/avg/max MHz over every core and burst sample, plus the hardware range) where cpufreq sysfs exists. `since=<timestamp>` (URL-encoded) returns only newer records. Every response has `cutoff` (the window start, for trimming what a client already holds) and `now` (the server's clock, for the next `since`), both epoch seconds. With `since`, `truncated: true` means the read stopped before reaching `since` (refetch without it) and `skipped_lines` counts the unreadable lines in everything the server has parsed, which can reach further back than the records returned |
| `GET /api/history/field/{name}` | One heavy field over the range, `[{timestamp, <name>}]`: `temps` (every sensor) or `processes` (the per-process rows, when logging is on). The history cards that draw these ask for them alone. Takes `since` like `/api/history` |
| `POST /api/history/record`   | Record now (rate-limited)                   |
| `GET /api/history/raw`       | `history.jsonl` as recorded (password): rates as capped when each record was taken, not re-capped |
| `GET /api/history/settings`  | Current recording settings                  |
| `POST /api/history/settings` | Update recording settings (password)        |
| `GET /config.json`           | The config, as written (including `http_targets` URLs) |
| `GET /themes/index.json`     | Theme names for the picker (`themes/*.css`) |
| `GET /text`                  | Console report: `curl "http://<host>:8080/text?width=$COLUMNS"` (query: `view` latest/history/both, `style` unicode/ascii/simple, `color` auto/on/256/16/off, `hours` default 1-3 by width and record interval, `width` default 80, `height`, `live`, `table`, `rows`) |
| `GET /tui.py`                | The live terminal client, served so it runs without a checkout |
| `GET /help`                  | Plain-text guide to `/text` and the JSON API, written for people and agents |

## Layout

```
horatio/
├── server.py                    # FastAPI server + Glances subprocess + history recorder
├── tests/                       # pytest suites, browser fixtures, and native Lua checks
├── scripts/                     # development helpers (native Lua/Nixio build, OpenWrt container test)
├── index.html, static/          # Frontend: app.js (polling, cards, history), config.js, charts.js, util.js, style.css
├── themes/                      # optional stylesheets for the Theme picker (README.md: how to write one)
├── config.example.json          # -> config.json (per machine, gitignored)
├── horatio.service              # systemd unit template (path and user rendered by install.sh)
├── install.sh                   # install / --update / --collector / --uninstall
├── VERSION, CHANGELOG.md        # the release number and what each release changed
├── collector/                   # root collectors, their units, config example, alarm table
├── nginx/                       # optional reverse proxy with subdomains per service
├── openwrt/                     # Lua backend serving the same frontend on OpenWrt routers
├── logs/                        # runtime: history.jsonl, server.log, glances.log
└── pyproject.toml, uv.lock
```

## Development

The test suite uses pytest. Node.js 18+ is required for the browser fixtures;
the live integration tests start the real dashboard and Glances on localhost.

```bash
uv sync --locked
make format         # Ruff fixes and formatting (changes Python files)
make check-format   # Ruff lint and formatting checks
make typing         # ty type checking
make test           # Linux pytest suite (same as make test-linux)
make check          # formatting, typing, and Linux tests; the default target
make test-openwrt   # native Lua tests; requires Lua 5.1 and Nixio
make test-openwrt-container   # install + --update on real OpenWrt in docker (CONTAINER=podman)
make test-all       # Linux and native Lua suites
make check-all      # formatting, typing, and both suites
```

Tests are grouped by responsibility:

- `tests/unit/`: history recovery, metrics, privacy, authentication, and text/TUI rendering.
- `tests/integration/`: HTTP contracts against a real dashboard and Glances.
- `tests/installers/` and `tests/collector/`: isolated service and hardware fixtures.
- `tests/frontend/`: Node.js fixtures for browser behavior and history caching.
- `tests/openwrt/`: native Lua checks, including history under UTC and America/New_York.

Pytest fixtures copy application code and `config.example.json` into temporary
directories before importing the server or starting it. Config, passwords, and
history from the checkout are never copied or changed. This isolation also
applies when invoking pytest directly; no snapshot wrapper is needed.

```bash
uv run --locked pytest tests/unit -q           # unit tests only
uv run --locked pytest -m 'not integration'   # omit the live HTTP server
make test PYTEST_ARGS='-k history -v'          # select tests by name
```

The default suite does not require Lua. `make test-openwrt` and `make check-all`
fail explicitly if native dependencies are unavailable; they never silently
skip the OpenWrt checks. CI runs both suites in separate jobs.

[GitHub Actions](.github/workflows/check.yml) runs these Make targets on pushes,
pull requests, and manual dispatches. Formatting, typing, Linux tests, and native
Lua tests run independently, so a formatting/type failure does not prevent the
tests from running. All jobs must pass. Avoid running the mutating `format`
target concurrently with other targets.

Python functions, variables, and fields have explicit type annotations; use
`Literal` for fixed sets of values. Ruff checks function annotation coverage and
`ty` checks type compatibility. Comprehension and lambda bindings are inferred
from their inputs because Python has no syntax for annotating them inline.

CI uses Ubuntu 24.04, Python 3.13, Node.js 22, `uv.lock`, and pinned action
revisions. The native Lua job uses OpenWrt's double/int32 number model, checks
large byte totals in API responses, and runs the history
tests under UTC and America/New_York. It does not boot OpenWrt or exercise real
router services. The workflow starts when these files are pushed to GitHub;
branch protection is configured separately in the repository settings.

The collectors are tested against hand-written fixture directories; recipe in
[`collector/README.md`](collector/README.md).

### OpenWrt

A small Lua backend serves the same frontend on OpenWrt routers without Python or Glances,
reading `/proc` and `/sys` directly:

```bash
cd openwrt
sh install.sh             # install and start on port 8080 (LAN only)
sh install.sh --update    # to the newest release (or --update vX.Y.Z), then reinstall
sh install.sh --uninstall
```

`--update` needs no git: it reads the newest version from `main`'s `VERSION` on GitHub,
downloads that release's tarball over HTTPS (needs `libustream-mbedtls` and `ca-bundle`, on
stock images), checks its `VERSION`, and copies it over the checkout; `www/`, `config.json`
and `logs/` are not in a release and stay. Files a release deleted stay behind in the
checkout; a deleted theme stays listed, since `www/` is rebuilt from the checkout.
`--update vX.Y.Z` always downloads and reinstalls that version, the current one included:
that is the repair after an update that stopped partway. The installer stamps the asset URLs in `www/index.html` with the version
(`?v=`), as uhttpd sends no cache headers and browsers would keep the old scripts.

Requires `uhttpd-mod-lua`, `luci-lib-jsonc`, and `luci-lib-nixio` (installed by the
script). On an apk-based OpenWrt the script refreshes the package index (`apk update`)
and upgrades `uhttpd` together with LuCI's uhttpd plugins, `uhttpd-mod-ubus` and
`uhttpd-mod-ucode`, when the router has them: a plugin left at an older version than
the server breaks whatever uses it (LuCI answers "Bad Gateway" behind nginx). After an
install, `apk list --installed | grep uhttpd` should show one version. uhttpd serves only `www/`, never the checkout: the installer copies
`index.html`, `static/` and `themes/` there (and lists the themes in `www/themes/index.json`),
so rerun it after updating or adding a theme. The config lives at
`www/config.json` (served as `/config.json`, read by the API; if it is not valid JSON the
backend runs on defaults and says so in `logread`); `config.json` in the
checkout root is a link to it. Edit `www/config.json`: tools that replace the file
(`sed -i`, save-by-rename editors) turn the link into a copy nobody reads. If both
copies exist, the next install run keeps the newer one, saves the other as
`config.json.bak`, and says so. An existing `config.json` is moved into `www/`
on install, and uninstall leaves both in place. A cron job requests `/api/history/tick` every
5 minutes and the tick that finds a record due takes it, so no dashboard request waits for the
second of sampling; if cron stops, records are taken on ordinary requests instead, up to two
minutes late. A `history.record_interval` under 5 minutes is finer than the tick, so
there ordinary requests record on time, and with no dashboard open records come every 5 minutes. A kernel file lock serializes recording and
rotation and releases automatically if a process exits. Rotation (gzipping the
older half of a full history file) runs in a background process, so recording
waits for it rather than a request timing out; one that fails is logged to syslog
and retried after the next record, and history is never deleted. It does not implement `since=` (Refresh reloads the whole window there)
or the rate caps on history.

Run the OpenWrt Lua tests from the checkout root:

```sh
make test-openwrt               # history runs under UTC and America/New_York
# A native Lua/Nixio build outside the system installation also works:
LUA_CPATH='/path/to/nixio/?.so;;' make test-openwrt LUA=/path/to/lua5.1
```

To build the same pinned Lua/Nixio dependencies used in CI without installing
system-wide modules (requires a C toolchain, Make, libcrypt development headers,
`curl`, Git, `tar`, and `patch`):

```sh
sh scripts/setup-lua-tests.sh /tmp/horatio-native-lua
LUA_CPATH='/tmp/horatio-native-lua/luci/libs/luci-lib-nixio/src/?.so;;' \
  make test-openwrt LUA=/tmp/horatio-native-lua/lua-5.1.5/src/lua
```

Use a fresh build directory when running the setup script again.

The tests use temporary files, forked contenders, and injected I/O failures;
they do not touch recorded history or the host's service configuration.
They require Lua 5.1, native Nixio, gzip, and timezone data. On an OpenWrt box
without GNU Make, run `lua tests/openwrt/test_handler.lua`,
`lua tests/openwrt/test_metrics.lua`, and `lua tests/openwrt/test_history.lua` directly.

The appropriate environment depends on the claim being tested:

| What is being checked | Environment | What it does not cover |
|---|---|---|
| Linux APIs and history, frontend logic, mocked installers/collectors | `make test-linux` on Linux with the locked Python environment and Node.js | Actual router services or GPU hardware |
| Lua recording locks, interrupted writes, and rotation | `make test-openwrt` on Linux with real Lua 5.1/Nixio and gzip | OpenWrt's service lifecycle or flash storage |
| Router install, `--update` and the dashboard answering | `make test-openwrt-container`: the official OpenWrt rootfs booted with procd in docker or podman, with its own UCI, uhttpd, apk feeds and BusyBox; CI runs it on every push | The nginx/LuCI port handoff, real network interfaces, flash storage |
| Router installation and service readiness | A disposable, booted OpenWrt VM with its own UCI, procd, uhttpd, nginx, and BusyBox | Every custom router configuration or physical flash behavior |

The Lua suite checks preservation after application/process failures, not
durability through sudden power loss. A host Nixio build is useful for the Lua
tests; the container covers installing and updating, not the nginx handoff.

<details>
<summary>Build the native Lua test environment on Linux without installing host packages</summary>

Requires a C toolchain, Make, Git, curl, patch, and libcrypt development files. The
[setup script](scripts/setup-lua-tests.sh) uses the [official Lua archive and checksum](https://www.lua.org/ftp/)
with pinned [OpenWrt number-model patches](https://github.com/openwrt/openwrt/tree/c9c78ac1c71995b1b467c3c5fe99527dbfaf7d71/package/utils/lua/patches)
and a pinned [OpenWrt Nixio source revision](https://github.com/openwrt/luci/tree/75a306df84a020c57a631ef5b4c02ace56d93387/libs/luci-lib-nixio).
Run from the Horatio checkout:

```sh
(
set -eu
native_dir=$(mktemp -d /tmp/horatio-native-lua-XXXXXX)
sh scripts/setup-lua-tests.sh "$native_dir"
LUA_CPATH="$native_dir/luci/libs/luci-lib-nixio/src/?.so;;" \
  make test-openwrt LUA="$native_dir/lua-5.1.5/src/lua"
)
```

This runs the existing tests with native processes and file locks, but still
uses the host's C library and utilities. It does not emulate a router.

</details>

For service integration, use a disposable VM of the OpenWrt release being
deployed. The [official x86 images](https://downloads.openwrt.org/releases/25.12.6/targets/x86/64/)
and [QEMU instructions](https://openwrt.org/docs/guide-user/virtualization/qemu)
provide a starting point; a 25.12 guest uses `apk`, while older supported
installations may use `opkg`. Boot the combined disk image with a writable
snapshot, two virtual NICs for LAN/WAN, and host forwards bound to loopback.
Inside a disposable 25.12 guest, install the test and service dependencies:

```sh
apk update
apk add lua luci-lib-nixio luci-lib-jsonc uhttpd uhttpd-mod-lua \
  luci nginx-ssl nginx-ssl-util make zoneinfo-northamerica
```

Copy the source checkout without local config/history, then run
`make test-openwrt LUA=lua`. Separately exercise real dashboard install, update,
and uninstall, plus the nginx/LuCI port handoff and a deliberately occupied port.
The Python installer fixtures remain mocks even inside a VM. Save the guest disk
and installed-package list if an exact environment must be repeated; package
feeds can change. This VM procedure has not yet been boot-tested for Horatio.

Chart.js (MIT) is fetched at install time; Glances (LGPL) is a Python dependency.
