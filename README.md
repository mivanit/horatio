Dashboard/logging of basic system info (cpu/ram/temps/disk io/network/etc) for homelabs.

Backend: FastAPI wrapping a [Glances](https://github.com/nicolargo/glances) subprocess plus
`nvidia-smi`, `ping`, `nethogs`, `pidstat`. Frontend: vanilla JS and Chart.js, no build step.

# Requirements

- Linux with systemd; [uv](https://docs.astral.sh/uv/). The optional-tool offers use
  `apt-get`; on other distros install `nethogs` + `libcap2-bin` and `sysstat` by hand.
- Optional, per card:

| Card | Needs |
|---|---|
| GPUs | an NVIDIA driver (`nvidia-smi`) |
| Network Processes | `nethogs` with file capabilities (the installer offers to set it up) |
| Disk I/O Processes | `sysstat` (`pidstat`) |
| System Mail | a local MTA delivering root's mail to the service user's spool: `postfix` ("Local only") plus `bsd-mailx`, with `root: <user>` in `/etc/aliases` and `newaliases` |
| Hardware health, Drive Health, Fan Speeds, Temperatures, Power | the root collector (`--collector`): `smartmontools` (required: without it the Hardware card alarms); optionally `ipmitool` + a BMC for fans/temps/system watts, Intel RAPL for CPU/DRAM watts and `nvidia-smi` for GPU memory temperatures and thermal alarms, skipped when absent. See [`collector/README.md`](collector/README.md) |

## Install

```bash
git clone https://github.com/mivanit/horatio ~/horatio
cd ~/horatio
./install.sh               # web service on :8080; run as your user, it calls sudo where needed
./install.sh --collector   # the same, plus the root hardware collector
```

It offers nethogs and sysstat until they are installed (`--no-optional` silences the offers).
Then edit `config.json` (created from `config.example.json`: title, ping targets, thresholds)
and `sudo systemctl restart horatio`; with the collector, also the collector's config at
`/etc/horatio/config.json` (see the collector README; it takes effect on the next 15-min run).
Open `http://<host>:8080`, or from a terminal `curl http://<host>:8080/text` (`/help` lists the options and API). For a live terminal view, see [Terminal](#terminal). The checkout can live anywhere writable by the user who runs it (no
spaces in the path); the service file is rendered from `horatio.service` with that path and user.

After install, `uv run python server.py --set-password` sets the password for the raw history
download and the settings endpoint (stored hashed in `.password`).

### Update

```bash
git pull && ./install.sh
```

Idempotent: re-syncs the venv, re-fetches Chart.js, re-renders the unit, restarts the service,
and re-installs the collector when it is installed on this box (its first run is synchronous,
a minute or three on a box with many drives). The optional-tool offers repeat until the tool
is installed, or pass `--no-optional`. `config.json`, `/etc/horatio/config.json`, `.password`
and `logs/` are never touched.

## Uninstall

```bash
./install.sh --uninstall
```

Removes the service, the collector's units, scripts and snapshots. The checkout, its
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
- **From a horatio checkout.** `python3 tui.py` on the box itself finds the local server (the port comes from the service file, else 8080). Elsewhere, `python3 tui.py http://<host>:8080`, or set `HORATIO_URL`.

`http://<host>:8080/help` has the full list of options, with the box's real address filled in.

## Configuration

`config.json` (per machine, gitignored):

| key | meaning |
|---|---|
| `title`, `title_emoji` | page title and heading |
| `ping_targets` | `[{address}]` pinged every poll and recorded to history |
| `history.record_interval` | seconds between history records (default 600) |
| `history.min_record_interval` | minimum seconds between manual "record now" requests, 0 to disable (default 60) |
| `history.sample_duration`, `history.sample_count` | each record averages `sample_count` samples over `sample_duration` seconds |
| `history.processes_enabled`, `history.process_count` | log the top `process_count` processes by CPU and by memory, plus any holding GPU memory, in each history record (`processes`: pid, name always; ppid, create_time, user, status, threads, cpu_percent, rss_mb, disk_read_kbps, disk_write_kbps, gpu_mem_mib + gpus, gpu_util_percent when known and non-zero); default off, 10. Roughly doubles a record's size at 10, so the 64 MB history tail covers fewer days |
| `thresholds.*_percent`, `temp_celsius`, `network_mbps`, `disk_io_mbps`, `ping_ms` | orange-card thresholds |
| `thresholds.power_max_watts`, `power_psu_watts`, `power_warn_fraction` | the power bar's right edge, its dashed PSU line, and the orange threshold as a fraction of the PSU |
| `limits.network_mbps`, `limits.disk_io_mbps` | ceilings (MB/s) for each network interface and each drive, because Glances' rates occasionally come out absurd (default 1250, i.e. 10 Gbit/s, and 15000). A sample over the cap counts as the cap, so a spike is shortened, not removed. Totals are the sums of the capped interfaces / drives and are not capped themselves (bridges, veths and tunnels count the same traffic more than once). Applies to the live cards, history and `/text`, not to the raw `/api/4/*` endpoints or to history on the OpenWrt backend. A record that hit a cap lists it under `rate_capped` (`metric`, `interface` or `device` + `drive`, `samples` of `of`, `raw_max`, `cap`) and the server logs it; each record notes the caps it was taken under in `rate_caps`. The server reads the limits at startup (restart after changing them); the page reads them on load. Records without `rate_caps` (recorded before the caps) are clamped when read; records with it are served as recorded, so a changed cap applies to new records only |

Runtime settings (poll interval, chart length, grid columns, card order) live in the browser.
History recording settings can also be changed from the page with the password; they revert to
`config.json` on restart.

## Security

Meant for a trusted LAN, not public interfaces. probably has many security holes. The dashboard binds to all interfaces and is unauthenticated except
the raw history download and the settings endpoint. Unauthenticated data includes local IP
addresses, mail subjects, drive models and serials, and the Glances metrics; the Glances
process views that carry full command lines are blocked (`/api/4/all`, `/api/4/processlist*`,
`/api/4/processes*`), and the dashboard's own process list strips arguments. The web process
runs as an unprivileged user under `ProtectSystem=strict` and never runs `sudo`, `smartctl` or
`ipmitool`: the collector runs as root on its own timers and the web process only reads the
JSON it writes. The IP card and each history record ask `api.ipify.org` for the public address.

again, don't put this on anything besides a trusted LAN!

## API

| Endpoint                     | Description                                 |
| ---------------------------- | ------------------------------------------- |
| `GET /`                      | Dashboard UI                                |
| `/api/4/*` (any method)      | Proxied to the Glances REST API (except the blocked process views above) |
| `GET /api/4/fs`              | Glances filesystems, deduplicated by device (bind mounts collapse) |
| `GET /api/processlist`       | Process list with arguments stripped (the one the UI uses) |
| `GET /api/list-endpoints`    | Which file-backed endpoints exist on this box (mail, storage-health, sensors) |
| `GET /api/gpu-info`          | NVIDIA GPU details                          |
| `GET /api/cpu-info`          | CPU details from /proc/cpuinfo              |
| `GET /api/gpu-processes`     | GPU process list: per (GPU, pid) memory plus `gpu_util_percent` / `gpu_mem_util_percent` from `nvidia-smi pmon` (null when the driver had no sample for that pid, pmon is unavailable or slow, or the GPU is past pmon's four) |
| `GET /api/network-processes` | Per-process network bandwidth (nethogs)     |
| `GET /api/disk-processes`    | Per-process disk I/O (pidstat)              |
| `GET /api/ping`              | Ping the configured targets                 |
| `GET /api/ip-info`           | LAN, public, and Tailscale IP addresses     |
| `GET /api/mail`              | Local system mail: `count` and the newest 10 `{date_epoch, subject}`; always 200 with `status` ok/missing/error |
| `GET /api/storage-health`    | The 15-min collector snapshot (`alarms`, `info`, drives, md, ECC, SEL, backup, failed units, journal errors) with `status` ok/stale/missing/error and `age_sec`; always 200 |
| `GET /api/sensors`           | The 60 s sensors snapshot (`fans`, `temps`, `power`, `gpus` with nvidia-smi temperatures, driver limits, thermal slowdown and ECC, `alarms`), same shape, stale after 300 s |
| `GET /api/history`           | History records (query: `hours`, default 24) without the heavy fields (`temps`, `processes`) and `ip`. Each record also carries `cpu_freq` (min/median/avg/max MHz over every core and burst sample, plus the hardware range) where cpufreq sysfs exists. `since=<timestamp>` (URL-encoded) returns only newer records. Every response has `cutoff` (the window start, for trimming what a client already holds) and `now` (the server's clock, for the next `since`), both epoch seconds. With `since`, `truncated: true` means the read stopped before reaching `since` (refetch without it) and `skipped_lines` counts only the part read |
| `GET /api/history/field/{name}` | One heavy field over the range, `[{timestamp, <name>}]`: `temps` (every sensor) or `processes` (the per-process rows, when logging is on). The history cards that draw these ask for them alone. Takes `since` like `/api/history` |
| `POST /api/history/record`   | Record now (rate-limited)                   |
| `GET /api/history/raw`       | `history.jsonl` as recorded (password): rates as capped when each record was taken, not re-capped |
| `GET /api/history/settings`  | Current recording settings                  |
| `POST /api/history/settings` | Update recording settings (password)        |
| `GET /config.json`           | The config                                  |
| `GET /text`                  | Console report: `curl "http://<host>:8080/text?width=$COLUMNS"` (query: `view` latest/history/both, `style` unicode/ascii/simple, `color` auto/on/256/16/off, `hours` default 1-3 by width and record interval, `width` default 80, `height`, `live`, `table`, `rows`) |
| `GET /tui.py`                | The live terminal client, served so it runs without a checkout |
| `GET /help`                  | Plain-text guide to `/text` and the JSON API, written for people and agents |

## Layout

```
horatio/
├── server.py                    # FastAPI server + Glances subprocess + history recorder
├── test.py                      # Integration tests (starts its own server)
├── index.html, static/          # Frontend: app.js (polling, cards, history), config.js, charts.js, util.js, style.css
├── config.example.json          # -> config.json (per machine, gitignored)
├── horatio.service              # systemd unit template (path and user rendered by install.sh)
├── install.sh                   # install / update / --collector / --uninstall
├── collector/                   # root collectors, their units, config example, alarm table
├── nginx/                       # optional reverse proxy with subdomains per service
├── openwrt/                     # Lua backend serving the same frontend on OpenWrt routers
├── logs/                        # runtime: history.jsonl, server.log, glances.log
└── pyproject.toml, uv.lock
```

## Development

```bash
cp config.example.json config.json   # once
uv sync
uv run python test.py                # integration tests: starts a server, records into logs/
uv run ruff check .
```

The collectors are tested against hand-written fixture directories; recipe in
[`collector/README.md`](collector/README.md).

### OpenWrt

A small Lua backend serves the same frontend on OpenWrt routers without Python or Glances,
reading `/proc` and `/sys` directly:

```bash
cd openwrt
sh install.sh             # install and start on port 8080 (LAN only)
sh install.sh --uninstall
```

Requires `uhttpd-mod-lua` and `luci-lib-jsonc` (installed by the script). It does not implement
`since=` (Refresh reloads the whole window there) or the rate caps on history.

Chart.js (MIT) is fetched at install time; Glances (LGPL) is a Python dependency.
