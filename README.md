# Horatio

Dashboard and history for homelab CPU, RAM, temperatures, disk I/O, network, GPUs,
and hardware health. [Live demo](https://mivanit.github.io/horatio/) uses the real
frontend with synthetic data ([how it works](docs/README.md)).

The default Python backend wraps [Glances](https://github.com/nicolargo/glances),
plus `nvidia-smi`, `ping`, `nethogs`, and `pidstat`. The frontend is vanilla JS and
Chart.js, with no build step. A smaller Lua backend supports
[Linux](lua/README.md#linux) and [OpenWrt](lua/README.md#openwrt) with fewer metrics
and no Python or Glances.

## Requirements

Choose a backend and installer for your platform:

| Platform / backend | Requirements | Installer |
|---|---|---|
| Linux / Python | systemd, [uv](https://docs.astral.sh/uv/), Python 3.10+ managed through uv; full metrics and optional hardware collector | `./install.sh` |
| Linux / Lua | systemd, sudo, and a C build toolchain; builds a private Lua/uhttpd runtime, no Python or Glances | `sh lua/install-linux.sh` |
| OpenWrt / Lua | OpenWrt with UCI and uhttpd; installer adds Lua modules using apk or opkg, no build toolchain needed | `sh lua/install-openwrt.sh` |

Both Lua versions provide CPU, memory, network, filesystems, temperatures, and
history. GPU, fan/power, process, service-check, and hardware-alarm features need
the Python backend. See [Lua requirements](lua/README.md) for platform packages.

On Python, most temperatures, fan speeds, and power readings come directly from
Linux hwmon without root. Optional features need:

| Feature | Needs |
|---|---|
| NVIDIA GPUs | NVIDIA driver and `nvidia-smi` |
| Network processes | `nethogs` and [process permissions](#process-permissions) |
| Disk I/O processes | `sysstat` (`pidstat`); extra permissions to see other users' processes |
| System mail | Local MTA delivering root's mail to the service user's spool |
| Drive/hardware health, BMC sensors, root-only power/temperature readings | [Root collector](collector/README.md), with `smartmontools`; other tools depend on hardware |

See [optional tools](docs/operations.md#optional-tools) for packages, mail setup,
and sensor-driver troubleshooting.

## Install

### Linux: Python

For the Python backend, run as your user; the installer calls sudo where needed:

```bash
git clone https://github.com/mivanit/horatio ~/horatio
cd ~/horatio
./install.sh               # web service on :8080
# Or include the root hardware collector:
./install.sh --collector
```

Edit `config.json`, created from [config.example.json](config.example.json), then
run `sudo systemctl restart horatio`. Open `http://<host>:8080`.
With the collector, also edit `/etc/horatio/config.json`; its changes take effect
on the next 15-minute run. See the [collector guide](collector/README.md).
The checkout path must have no whitespace or `: % \ | &` characters.

The installer offers to install nethogs and sysstat using `apt-get`, but grants
no extra permissions by default. `--no-optional` silences the offers; on other
distros install `nethogs`, `libcap2-bin`, and `sysstat` yourself.

#### Process permissions

By default, Network Processes shows an error and Disk I/O Processes shows only
the service user's processes. `./install.sh --grant-capabilities` enables both
tools to see other users' activity. These capabilities apply to **every local
user** running those binaries, and a bug in either could allow reading any file.
See [permissions and removal](docs/operations.md#process-permissions) before enabling.

### Linux: Lua

Clone the repository as above, install the [build prerequisites](lua/README.md#linux),
then run as your user from the checkout root:

```bash
sh lua/install-linux.sh
```

Open `http://localhost:8080`. It listens on loopback by default; see the
[Linux Lua guide](lua/README.md#linux) for a LAN address. Edit `www/config.json`
and restart with `sudo systemctl restart horatio-lua.service`.

### OpenWrt: Lua

Copy or extract the repository onto the router, then run as root from the checkout root:

```sh
sh lua/install-openwrt.sh
```

Open `http://<router-LAN-IP>:8080`. Edit `www/config.json`; see the
[OpenWrt guide](lua/README.md#openwrt) for dependencies and updates. The Python
and systemd installers above are for ordinary Linux, not OpenWrt.

## Security

Use a trusted LAN. The Python dashboard listens on all interfaces and exposes
metrics, local IPs, mail subjects, and drive details without authentication.
`config.json` is served to visitors: put no passwords or tokens in it.

```bash
uv run python python/server.py --set-password
```

This sets the password for raw history downloads, recording-settings changes,
and alarm retirement. It does **not** protect the dashboard. See
[access and security](docs/operations.md#access-and-security) for the service
sandbox and process-data privacy details.

## Update and uninstall

For the Python backend:

```bash
./install.sh --update      # newest release
./install.sh --uninstall
```

Updates preserve configuration, `.password`, and history, and reinstall an
existing collector. Uninstall removes services, collector scripts/snapshots,
and process capabilities; the checkout, config, and history stay.

A checkout following `main` can use `git pull && ./install.sh`. Release checkouts
use `--update`, since they have a detached HEAD. See [versions and updates](docs/operations.md#versions-and-updates)
for selecting an older release and update restrictions, and [CHANGELOG.md](CHANGELOG.md)
for release notes.

Lua uses its platform installer: [Linux updates/removal](lua/README.md#linux)
or [OpenWrt updates/removal](lua/README.md#openwrt).

## Terminal

The Python backend serves a one-off report and a live terminal client:

```bash
curl "http://<host>:8080/text?width=$COLUMNS"
curl http://<host>:8080/text?style=simple       # plain ASCII for scripts/agents
curl -fsS http://<host>:8080/tui.py | python3 - # live view; q quits
```

The live command executes the script served by your box; use it with a server
you trust. The client needs Python 3.8+ on Linux or macOS, with no extra packages.
See [terminal usage](docs/operations.md#terminal) for downloading first, running
from a checkout, and overrides. `/help` on the running box lists options with its
address filled in.

## Configuration

Set the title, targets, history recording, thresholds, and supported sensor rules
in `config.json` (Python) or `www/config.json` (Lua). HTTP service checks are
Python-only. Restart the Python service after editing its config. Poll interval, chart
length, grid layout, card order, and theme live in the browser. Password-protected
recording settings changed through the page revert to config on restart.

The [configuration reference](docs/configuration.md) lists defaults and backend
differences. [Themes](themes/README.md) explains adding a stylesheet. For retained
alarms, event logs, or moving history to another box, see
[operations](docs/operations.md#logs-and-history).

## API

The [API reference](docs/api.md) covers metrics, alarms, services, history,
authentication, terminal output, and Lua differences. Start with `/api/version`,
`/api/alarms`, or `/api/history`; `/help` is also available as plain text.

## Development

Run `uv sync --locked` then `make check` for formatting checks, typing, and the
Python test suite; browser fixtures also need Node.js 18+. The
[development guide](docs/development.md) covers the source layout, focused tests,
Lua/OpenWrt test environments, CI, and releases.

## Documentation

- [API](docs/api.md) · [Configuration](docs/configuration.md) · [Operations](docs/operations.md)
- [Development](docs/development.md) · [Lua/Linux and OpenWrt](lua/README.md)
- [Root collector](collector/README.md) · [Themes](themes/README.md) · [Nginx reverse proxy](nginx/README.md)
- [Demo](docs/README.md) · [Changelog](CHANGELOG.md)

Chart.js (MIT) is fetched at install time; Glances (LGPL) is a Python dependency.
