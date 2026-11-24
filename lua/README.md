# Lua backend

[Horatio](../README.md) · [Configuration](../docs/configuration.md) · [API differences](../docs/api.md#lua-backend-differences) · [Development and tests](../docs/development.md#lua-tests)

Commands run from the checkout root unless they explicitly change directory.

A small Lua backend serves the same frontend without Python or Glances, reading
Linux `/proc` and `/sys` directly. Both installers use uhttpd and the same Lua
metrics/history implementation. It provides core CPU, memory, network, filesystem,
temperature and history data; it does not collect the Python backend's GPU, fan,
power, host-alarm or root-collector data.

## Linux

On ordinary Linux with systemd, run as the user who owns the checkout:

```bash
# Build prerequisites on Debian/Ubuntu (use equivalent packages on other distros):
sudo apt-get install build-essential cmake pkg-config curl ca-certificates libcrypt-dev wget
sh lua/install-linux.sh
# Optional: listen on a specific LAN address instead of the default loopback address.
LISTEN_ADDRESS=192.168.1.10 PORT=8080 sh lua/install-linux.sh
# Update code, then reinstall (repeat any LISTEN_ADDRESS/PORT overrides):
git pull && sh lua/install-linux.sh
sh lua/install-linux.sh --uninstall
```

The installer builds pinned Lua, Nixio, JSON and uhttpd dependencies privately in
`.lua-runtime/`; it does not install OpenWrt's package manager, UCI or procd. A changed
build recipe triggers a rebuild. Building needs a C toolchain and internet access;
running needs neither Python nor development packages. The supported service manager
is systemd; package installation instructions above are for Debian/Ubuntu. Other
systemd distributions need equivalent build prerequisites installed first.

The service is `horatio-lua.service`, runs as the installing user, and defaults to
`127.0.0.1:8080`. It serves only `www/`, with directory listings disabled. Use
`journalctl -u horatio-lua.service` for diagnostics and
`sudo systemctl restart horatio-lua.service` after editing `www/config.json`.
`horatio-lua-history.timer` requests a history tick every five minutes. Uninstall
removes the services and keeps the checkout, runtime, config and history. Python
and Lua cannot share a listening port or write the same history concurrently; use
separate checkouts and ports when comparing them. Lua HTTP has no authentication;
use a trusted network or an authenticated reverse proxy for access beyond loopback.

## OpenWrt

On OpenWrt, use its separate installer:

```bash
cd lua
sh install.sh             # install and start on port 8080 (LAN only)
sh install.sh --update    # to the newest release (or --update vX.Y.Z), then reinstall
sh install.sh --uninstall
```

The old `openwrt/install.sh` path forwards to `lua/install.sh` so existing
installations can upgrade. Updates select the installer shipped by the target
release, including older releases that used the old directory.

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
so rerun it after updating or adding a theme.

### Configuration

The config lives at `www/config.json` (served as `/config.json`, read by the API; if it is not valid JSON the
backend runs on defaults and says so in `logread`); `config.json` in the
checkout root is a link to it. Edit `www/config.json`: tools that replace the file
(`sed -i`, save-by-rename editors) turn the link into a copy nobody reads. If both
copies exist, the next install run keeps the newer one, saves the other as
`config.json.bak`, and says so. An existing `config.json` is moved into `www/`
on install, and uninstall leaves both in place.

### History

A cron job requests `/api/history/tick` every 5 minutes and the tick that finds a record due takes it, so no dashboard request waits for the
second of sampling; if cron stops, records are taken on ordinary requests instead, up to two
minutes late. A `history.record_interval` under 5 minutes is finer than the tick, so
there ordinary requests record on time, and with no dashboard open records come every 5 minutes. A kernel file lock serializes recording and
rotation and releases automatically if a process exits. Rotation (gzipping the
older half of a full history file) runs in a background process, so recording
waits for it rather than a request timing out; one that fails is logged to syslog
and retried after the next record, and history is never deleted. Lua does not
implement `since=` (Refresh reloads the whole window there) or the rate caps on
history; see [Lua API differences](../docs/api.md#lua-backend-differences).

For native tests, HTTP runtime tests, and container/VM checks, see the
[development guide](../docs/development.md#lua-tests).
