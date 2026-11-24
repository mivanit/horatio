# Development

[Horatio](../README.md) · [Lua installation](../lua/README.md) · [Demo](README.md)

Run commands from the checkout root.

## Tests and checks

The test suite uses pytest. Node.js 18+ is required for the browser fixtures;
the live integration tests start the real dashboard and Glances on localhost.

```bash
uv sync --locked
uv run --locked pytest --durations=25  # the 25 slowest tests
make format         # Ruff fixes and formatting (changes Python files)
make check-format   # Ruff lint and formatting checks
make typing         # ty type checking
make test           # Linux pytest suite (same as make test-python)
make check          # formatting, typing, and Linux tests; the default target
make test-lua       # native Lua tests; requires Lua 5.1 and Nixio
make test-openwrt-container   # install + --update on real OpenWrt in docker (CONTAINER=podman)
make test-all       # Linux and native Lua suites
make check-all      # formatting, typing, and both suites
```

Tests are grouped by responsibility:

- `tests/unit/`: history recovery, metrics, privacy, authentication, and text/TUI rendering.
- `tests/integration/`: HTTP contracts against a real dashboard and Glances.
- `tests/installers/` and `tests/collector/`: isolated service and hardware fixtures.
- `tests/frontend/`: Node.js fixtures for browser behavior and history caching.
- `tests/lua/`: native Lua checks, including history under UTC and America/New_York.

Pytest fixtures copy application code and `config.example.json` into temporary
directories before importing the server or starting it. Config, passwords, and
history from the checkout are never copied or changed. This isolation also
applies when invoking pytest directly; no snapshot wrapper is needed.

```bash
uv run --locked pytest tests/unit -q           # unit tests only
uv run --locked pytest -m 'not integration'   # omit the live HTTP server
make test PYTEST_ARGS='-k history -v'          # select tests by name
```

The default suite does not require Lua. `make test-lua` and `make check-all`
fail explicitly if the native Lua/Nixio dependencies are unavailable. The additional
HTTP runtime tests require `LUA_RUNTIME`; they are skipped when it is unset and run
in a separate CI job that builds the private Linux runtime.

[GitHub Actions](../.github/workflows/check.yml) runs these Make targets on pushes,
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
[`collector/README.md`](../collector/README.md).

## Lua tests

Run the Lua tests from the checkout root:

```sh
make test-lua                   # history runs under UTC and America/New_York
# A native Lua/Nixio build outside the system installation also works:
LUA_CPATH='/path/to/nixio/?.so;;' make test-lua LUA=/path/to/lua5.1
```

The tests use temporary files, forked contenders, and injected I/O failures;
they do not touch recorded history or the host's service configuration.
They require Lua 5.1, native Nixio, gzip, and timezone data. On an OpenWrt box
without GNU Make, run `lua tests/lua/test_handler.lua`,
`lua tests/lua/test_metrics.lua`, and `lua tests/lua/test_history.lua` directly.

### Build native Lua test dependencies

Build the pinned Lua/Nixio dependencies used in CI without installing system-wide
modules. Requires a C toolchain, Make, Git, curl, tar, patch, and libcrypt
development files. The [setup script](../scripts/setup-lua-tests.sh) uses the [official Lua archive and checksum](https://www.lua.org/ftp/)
with pinned [OpenWrt number-model patches](https://github.com/openwrt/openwrt/tree/c9c78ac1c71995b1b467c3c5fe99527dbfaf7d71/package/utils/lua/patches)
and a pinned [OpenWrt Nixio source revision](https://github.com/openwrt/luci/tree/75a306df84a020c57a631ef5b4c02ace56d93387/libs/luci-lib-nixio).
Run from the Horatio checkout:

```sh
(
set -eu
native_dir=$(mktemp -d /tmp/horatio-native-lua-XXXXXX)
sh scripts/setup-lua-tests.sh "$native_dir"
LUA_CPATH="$native_dir/luci/libs/luci-lib-nixio/src/?.so;;" \
  make test-lua LUA="$native_dir/lua-5.1.5/src/lua"
)
```

The recipe creates a fresh build directory each time. This runs the existing
tests with native processes and file locks, but still uses the host's C library and utilities. It does not emulate a router.

### HTTP runtime tests

To build and test the runtime without installing a systemd service:

```bash
sh scripts/setup-lua-runtime.sh /tmp/horatio-lua-runtime
LUA_RUNTIME=/tmp/horatio-lua-runtime uv run --locked pytest tests/lua/test_runtime.py
```

### Installed-service and migration tests

CI runs `tests/deployment/test_systemd.py` on separate disposable Ubuntu VMs for
Python and Lua. These invoke the real installers from a checkout under the user's
home, with the shipped systemd restrictions. Python covers fresh installation and
v0.2.1 migration, including credentials, configuration, history and existing root
collectors. Lua checks service restart and unattended recording by the actual
five-minute timer; that test intentionally takes several minutes.

These tests install system services and require a disposable systemd host,
passwordless sudo, the documented backend prerequisites, and a uv-managed Python
under the invoking user's home. They refuse an existing Horatio installation and
are excluded from the default test suite. To run one backend:

```bash
HORATIO_SYSTEMD_TEST=1 uv run --locked pytest -v tests/deployment/test_systemd.py -k python
HORATIO_SYSTEMD_TEST=1 uv run --locked pytest -v tests/deployment/test_systemd.py -k lua
```

The OpenWrt migration scenario installs the actual v0.2.1 tag alongside LuCI,
then applies the documented manual source update and renamed installer. It checks
configuration and history preservation, including a gzip archive, continued
recording after restart, and LuCI HTTP responses. Fetch tags before running it:

```bash
git fetch --tags
scripts/openwrt-container-test.sh migrate
```

### Choose a test environment

The appropriate environment depends on the claim being tested:

| What is being checked | Environment | What it does not cover |
|---|---|---|
| Linux APIs and history, frontend logic, mocked installers/collectors | `make test-python` on Linux with the locked Python environment and Node.js | Actual router services or GPU hardware |
| Lua recording locks, interrupted writes, and rotation | `make test-lua` on Linux with real Lua 5.1/Nixio and gzip | OpenWrt's service lifecycle or flash storage |
| Installed Python/Lua services, Python upgrade, unattended Lua timer | Opt-in `tests/deployment/test_systemd.py` on disposable systemd VMs; CI runs both backends | Real GPU, disk and BMC hardware |
| Router fresh install, `--update`, migration into the candidate and LuCI coexistence | `scripts/openwrt-container-test.sh fresh` / `migrate`: the official OpenWrt rootfs booted with procd in docker or podman, with its own UCI, uhttpd, LuCI, apk feeds and BusyBox; CI runs both scenarios | The nginx/LuCI port handoff, real network interfaces, flash storage |
| Router installation and service readiness | A disposable, booted OpenWrt VM with its own UCI, procd, uhttpd, nginx, and BusyBox | Every custom router configuration or physical flash behavior |

The Lua suite checks preservation after application/process failures, not
durability through sudden power loss. A host Nixio build is useful for the Lua
tests; the container covers installing and updating, not the nginx handoff.

### OpenWrt VM checks

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
`make test-lua LUA=lua`. Separately exercise real dashboard install, update,
and uninstall, plus the nginx/LuCI port handoff and a deliberately occupied port.
The Python installer fixtures remain mocks even inside a VM. Save the guest disk
and installed-package list if an exact environment must be repeated; package
feeds can change. This VM procedure has not yet been boot-tested for Horatio.

## Releases

To cut a release: `make release V=X.Y.Z` (writes `VERSION`, dates the changelog's
Unreleased section, commits and tags), then `git push origin main vX.Y.Z`.

## Layout

```
horatio/
├── python/                      # FastAPI + Glances server, sensor/host checks, terminal client
├── tests/                       # pytest suites, browser fixtures, and native Lua checks
├── scripts/                     # development helpers (native Lua/Nixio build, OpenWrt container test)
├── index.html, static/          # Frontend: app.js (polling, cards, history), config.js, charts.js, util.js, style.css
├── docs/                        # reference guides and demo sources
├── themes/                      # optional stylesheets for the Theme picker (README.md: how to write one)
├── config.example.json          # -> config.json (per machine, gitignored)
├── horatio.service              # systemd unit template (path and user rendered by install.sh)
├── install.sh                   # install / --update / --collector / --uninstall
├── VERSION, CHANGELOG.md        # the release number and what each release changed
├── collector/                   # root collectors, their units, config example, alarm table
├── nginx/                       # optional reverse proxy with subdomains per service
├── lua/                         # Lua backend; install-linux.sh and install-openwrt.sh
├── logs/                        # runtime: history.jsonl, server.log, glances.log
└── pyproject.toml, uv.lock
```

Chart.js (MIT) is fetched at install time; Glances (LGPL) is a Python dependency.
