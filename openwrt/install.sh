#!/bin/sh
# OpenWrt dashboard installer
# Configures a uhttpd instance on port 8080 (LAN only) to serve
# the dashboard with Lua API handler.
#
# Usage:
#   sh install.sh              # install and start
#   sh install.sh --update [vX.Y.Z]
#                              # move the checkout to the newest release (or the one
#                              # named), then install it
#   sh install.sh --uninstall  # remove dashboard config

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# uhttpd serves only this directory: index.html and static/ are copied in on
# each install, and www/config.json is the real config (DASHBOARD_DIR/config.json
# links to it). Serving the checkout itself would expose .git/, logs/ and source.
WWW_DIR="$DASHBOARD_DIR/www"
CONFIG="$DASHBOARD_DIR/config.json"
WWW_CONFIG="$WWW_DIR/config.json"
UCI_SECTION="dashboard"
PORT=8080

info()  { printf "[+] %s\n" "$1"; }
warn()  { printf "[!] %s\n" "$1"; }
error() { printf "[-] %s\n" "$1"; }

# The checkout's release, from VERSION; empty when it is missing.
read_version() {   # <dir>
    [ -f "$1/VERSION" ] && tr -d '[:space:]' < "$1/VERSION"
}

# Replace only our tagged job. OpenWrt keeps user tables in /etc/crontabs;
# distinguish an absent table from a failed read before writing anything.
update_history_cron() (
    cron_file="/etc/crontabs/$(id -un)"
    cron_previous=""
    if [ -e "$cron_file" ] || [ -L "$cron_file" ]; then
        cron_previous=$(crontab -c /etc/crontabs -l) || exit 1
    fi
    cron_tmp=$(mktemp) || exit 1
    trap 'rm -f "$cron_tmp"' EXIT
    trap 'exit 1' HUP INT TERM
    printf '%s\n' "$cron_previous" | sed '/ # dashboard-history[[:space:]]*$/d' > "$cron_tmp" || exit 1
    [ -z "$1" ] || printf '%s\n' "$1" >> "$cron_tmp" || exit 1
    crontab -c /etc/crontabs "$cron_tmp" || exit 1
    # BusyBox crontab can return success even when creating its new file fails.
    cron_current=$(crontab -c /etc/crontabs -l) || exit 1
    [ "$cron_current" = "$(cat "$cron_tmp")" ] || {
        error "Cron table was not updated."
        exit 1
    }
)

# --- Uninstall ---
if [ "$1" = "--uninstall" ]; then
    info "Removing dashboard configuration..."
    # A missing dashboard section is fine; package, save and restart errors are not.
    uci show uhttpd >/dev/null
    if uci -q get "uhttpd.$UCI_SECTION" >/dev/null; then
        uci delete "uhttpd.$UCI_SECTION"
    fi
    uci commit uhttpd
    /etc/init.d/uhttpd restart
    update_history_cron ""
    # Remove the served copies; keep www/config.json and its config.json link.
    rm -rf "$WWW_DIR/static" "$WWW_DIR/themes" "$WWW_DIR/index.html"
    info "Dashboard removed. Config kept: $WWW_DIR/config.json"
    exit 0
fi

# --- Help ---
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat <<EOF
Usage: sh install.sh              # install / update dashboard
       sh install.sh --update [vX.Y.Z]
                                  # move to the newest (or named) release, then install
       sh install.sh --uninstall  # remove dashboard config

Configures a uhttpd instance on port $PORT (LAN only) serving
the dashboard with Lua API handler.

Override LAN IP:  LAN_IP=192.168.1.1 sh install.sh
EOF
    exit 0
fi

# --- Update ---
# Releases are vX.Y.Z tags (README: Versions and updates). VERSION changes only in a
# release, so main's VERSION names the newest one. Its tarball is copied over the
# checkout (config.json, www/ and logs/ are not in a release), then its installer runs.
RAW_URL="https://raw.githubusercontent.com/mivanit/horatio"
TARBALL_URL="https://codeload.github.com/mivanit/horatio/tar.gz/refs/tags"
die() { error "$1"; exit 1; }
update_checkout() {   # [vX.Y.Z]
    old=$(read_version "$DASHBOARD_DIR") || old=unknown
    new="${1#v}"
    # Never --no-check-certificate: this download becomes code that runs as root.
    [ -n "$new" ] || new=$(wget -qO- "$RAW_URL/main/VERSION" | tr -d '[:space:]') || new=""
    [ -n "$new" ] || die "Could not reach GitHub (HTTPS needs libustream-mbedtls and ca-bundle)."
    [ -n "$1" ] || [ "$new" != "$old" ] || { info "Already at v$old."; exit 0; }
    # Unpack beside the checkout: same filesystem, and not in RAM-backed /tmp.
    # Left by an update that was killed (power loss): mktemp names only.
    rm -rf "$DASHBOARD_DIR"/.update.??????
    work=$(mktemp -d "$DASHBOARD_DIR/.update.XXXXXX") || die "mktemp failed."
    trap 'rm -rf "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    wget -qO "$work/r.tgz" "$TARBALL_URL/v$new" || die "No release v$new to download."
    # gzip | tar rather than tar -z, which some BusyBox builds lack.
    gzip -t "$work/r.tgz" || die "Download of v$new is incomplete; nothing changed."
    gzip -dc "$work/r.tgz" | tar -xf - -C "$work" || die "Could not unpack v$new."
    # GitHub's archive of tag vX.Y.Z is one directory, horatio-X.Y.Z.
    [ "$(read_version "$work/horatio-$new")" = "$new" ] || die "Unexpected download; nothing changed."
    # VERSION goes last: a copy that failed partway must not read as "Already at".
    mv "$work/horatio-$new/VERSION" "$work/VERSION"
    cp -R "$work/horatio-$new/." "$DASHBOARD_DIR/" && cp "$work/VERSION" "$DASHBOARD_DIR/VERSION" \
        || die "Copying v$new failed partway; rerun: sh $0 --update v$new"
    rm -rf "$work"
    trap - EXIT HUP INT TERM
    info "Updated v$old -> v$new. To go back: sh $0 --update v$old"
    # A bare --update would now say "Already at": the files are new, the install is not.
    info "If the install below fails, rerun it with: sh $0"
    # The release's installer does the install, so it matches what it installs.
    exec sh "$SCRIPT_DIR/install.sh"
}
if [ "$1" = "--update" ]; then
    update_checkout "${2:-}"
fi

# --- Preflight ---
command -v uci    >/dev/null 2>&1 || { error "uci not found. Requires OpenWrt."; exit 1; }
command -v uhttpd >/dev/null 2>&1 || { error "uhttpd not found."; exit 1; }

# --- Dependencies ---
info "Checking dependencies..."
# Stock OpenWrt has no timeout; without it a hung df or tailscale call blocks
# every request (metrics.lua). Optional: the dashboard runs without it.
NO_TIMEOUT="Could not install coreutils-timeout; a hung command can stall the dashboard."
if command -v apk >/dev/null 2>&1; then
    # apk's package index is in RAM and gone after a reboot: without it the
    # upgrade below fails or, worse, resolves against a stale index.
    apk update || warn "apk update failed; the installs below may fail or use an old package index"
    # uhttpd and its plugins, all at the feed's version and in ONE transaction: a
    # plugin from another version than the server gets a garbled handler path.
    # That is the Lua plugin (the dashboard never starts) and equally the ubus
    # and ucode plugins LuCI needs: an old uhttpd-mod-ubus left beside a new
    # uhttpd kills LuCI's backend at its first request. Only the ones already
    # installed are named, so a router without LuCI does not gain them.
    plugins=""
    for plugin in uhttpd-mod-ubus uhttpd-mod-ucode; do
        apk list --installed 2>/dev/null | grep -q "^$plugin-[0-9]" && plugins="$plugins $plugin"
    done
    # shellcheck disable=SC2086  # $plugins is a list of words
    apk add --upgrade uhttpd uhttpd-mod-lua $plugins luci-lib-jsonc luci-lib-nixio
    command -v timeout >/dev/null 2>&1 || apk add coreutils-timeout || warn "$NO_TIMEOUT"
elif command -v opkg >/dev/null 2>&1; then
    # opkg's package lists are in RAM and gone after a reboot: without them
    # every install fails with "Unknown package". Fetched once, only if needed.
    opkg_get() {
        opkg list-installed | grep -q "$1" && return 0
        [ -n "${opkg_updated:-}" ] || { opkg update || warn "opkg update failed; installs below may fail with 'Unknown package'"; opkg_updated=1; }
        opkg install "$1"
    }
    opkg_get uhttpd-mod-lua
    opkg_get luci-lib-jsonc
    opkg_get luci-lib-nixio
    command -v timeout >/dev/null 2>&1 || opkg_get coreutils-timeout || warn "$NO_TIMEOUT"
fi

# --- Chart.js ---
# Keep version/hash in sync with the Linux installer. Verify the existing asset
# too: a failed older installer may have left an empty or partial download.
CHARTJS_VERSION="4.5.1"
CHARTJS_SHA256="48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a"
CHARTJS="$DASHBOARD_DIR/static/chart.umd.min.js"
if printf '%s  %s\n' "$CHARTJS_SHA256" "$CHARTJS" | sha256sum -c - >/dev/null 2>&1; then
    info "Chart.js $CHARTJS_VERSION already present."
else
    info "Downloading Chart.js $CHARTJS_VERSION..."
    CHARTJS_TMP="$(mktemp "$DASHBOARD_DIR/static/.chart.XXXXXX")"
    trap 'rm -f "$CHARTJS_TMP"' EXIT
    trap 'exit 1' HUP INT TERM
    wget -qO "$CHARTJS_TMP" \
        "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js" || {
        error "Failed to download Chart.js; the existing asset was not replaced."
        exit 1
    }
    printf '%s  %s\n' "$CHARTJS_SHA256" "$CHARTJS_TMP" | sha256sum -c - >/dev/null 2>&1 || {
        error "Chart.js checksum mismatch; refusing to install an incomplete or unexpected file."
        exit 1
    }
    chmod 644 "$CHARTJS_TMP"
    mv "$CHARTJS_TMP" "$CHARTJS"
    trap - EXIT HUP INT TERM
fi

# --- Detect LAN IP ---
if [ -z "${LAN_IP:-}" ]; then
    LAN_IP=$(uci get network.lan.ipaddr 2>/dev/null || echo "")
fi
LAN_IP="${LAN_IP%%/*}"
if [ -z "$LAN_IP" ]; then
    # Fallback: first non-loopback IPv4
    LAN_IP=$(ip -4 addr show | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | cut -d/ -f1)
    [ -z "$LAN_IP" ] || warn "network.lan.ipaddr is not set in uci; guessed $LAN_IP (first non-loopback address). If that is not the LAN: LAN_IP=192.168.1.1 sh $0"
fi
if [ -z "$LAN_IP" ]; then
    error "Could not detect LAN IP. Set it manually: LAN_IP=192.168.1.1 sh $0"
    exit 1
fi
info "Binding to $LAN_IP:$PORT"

# --- Web root ---
# Refresh the served copies; config.json in www/ is never touched here.
info "Copying frontend into $WWW_DIR..."
mkdir -p "$WWW_DIR"
rm -rf "$WWW_DIR/static" "$WWW_DIR/index.html"
# uhttpd sends no Cache-Control for files, so browsers may keep old scripts for
# days after an update: give the asset URLs this install's version and time.
# The theme URL is built by the loader in <head> ('themes/' + t + '.css'), so a
# new or edited theme also reaches browsers once this script reruns.
ASSET_STAMP="$(read_version "$DASHBOARD_DIR" || true)-$(date +%s)"
sed -E \
    -e "s#\"(/static/[^\"?]+\.(js|css))\"#\"\1?v=$ASSET_STAMP\"#g" \
    -e "s#(\+ t \+ '\.css)\"#\1?v=$ASSET_STAMP\"#" \
    "$DASHBOARD_DIR/index.html" > "$WWW_DIR/index.html"
cp -RL "$DASHBOARD_DIR/static" "$WWW_DIR/static"
# Themes are copied like static/ (add one to themes/ and rerun this script);
# index.json lists them for the page's picker. The name rule matches server.py
# and index.html's loader; uhttpd serves no symlinks, hence cp -L.
rm -rf "$WWW_DIR/themes"
mkdir -p "$WWW_DIR/themes"
theme_names=""
for theme in "$DASHBOARD_DIR"/themes/*.css; do
    [ -f "$theme" ] || continue
    name=$(basename "$theme" .css)
    case "$name" in
        ''|default|*[!A-Za-z0-9_-]*) warn "Skipping theme $theme (name must be letters, digits, _ or -)"; continue ;;
    esac
    cp -L "$theme" "$WWW_DIR/themes/$name.css"
    theme_names="$theme_names${theme_names:+, }\"$name\""
done
printf '[%s]\n' "$theme_names" > "$WWW_DIR/themes/index.json"

# Config must exist before uhttpd loads the Lua handler at startup. Preserve
# the machine's existing config on updates, as the Linux installer does. The
# frontend and the Lua handler read www/config.json; DASHBOARD_DIR/config.json is a
# link to it, outside the web root. A regular file there is either a pre-www/
# config (first migration) or a copy left by a tool that replaced the link
# (sed -i, rsync, save-by-rename editors). Either copy may hold the latest edits,
# so the newer one is kept and the other is saved as config.json.bak, never lost.
if [ -L "$CONFIG" ]; then
    if [ "$(readlink "$CONFIG")" != "www/config.json" ]; then
        if [ ! -e "$WWW_CONFIG" ] && [ -f "$CONFIG" ]; then
            cp "$CONFIG" "$WWW_CONFIG"
        fi
        rm -f "$CONFIG"
    fi
elif [ -f "$CONFIG" ]; then
    if [ ! -e "$WWW_CONFIG" ] || cmp -s "$CONFIG" "$WWW_CONFIG" 2>/dev/null; then
        mv -f "$CONFIG" "$WWW_CONFIG"
        info "Moved config.json to $WWW_CONFIG (config.json now links to it)."
    elif [ "$CONFIG" -nt "$WWW_CONFIG" ]; then
        mv -f "$WWW_CONFIG" "$CONFIG.bak"
        mv -f "$CONFIG" "$WWW_CONFIG"
        warn "Two configs: kept the newer $CONFIG as $WWW_CONFIG; the older one is now $CONFIG.bak."
    else
        mv -f "$CONFIG" "$CONFIG.bak"
        warn "Two configs: kept the newer $WWW_CONFIG; the stray copy at $CONFIG is now $CONFIG.bak."
    fi
elif [ -e "$CONFIG" ]; then
    error "$CONFIG is not a regular file; move it aside and rerun."
    exit 1
fi
if [ ! -f "$WWW_CONFIG" ]; then
    cp "$DASHBOARD_DIR/config.example.json" "$WWW_CONFIG"
    info "Created $WWW_CONFIG from config.example.json: edit it (title, ping targets, thresholds)."
fi
[ -L "$CONFIG" ] || ln -s www/config.json "$CONFIG"

# --- Configure uhttpd ---
info "Configuring uhttpd dashboard instance..."
uci delete "uhttpd.$UCI_SECTION" 2>/dev/null || true

uci set "uhttpd.$UCI_SECTION=uhttpd"
uci add_list "uhttpd.$UCI_SECTION.listen_http=$LAN_IP:$PORT"
uci add_list "uhttpd.$UCI_SECTION.listen_http=127.0.0.1:$PORT"
uci set "uhttpd.$UCI_SECTION.home=$WWW_DIR"
uci set "uhttpd.$UCI_SECTION.lua_prefix=/api"
uci set "uhttpd.$UCI_SECTION.lua_handler=$SCRIPT_DIR/handler.lua"
uci set "uhttpd.$UCI_SECTION.max_requests=50"
uci set "uhttpd.$UCI_SECTION.no_symlinks=1"
uci set "uhttpd.$UCI_SECTION.no_dirlists=1"

uci commit uhttpd

# --- Restart uhttpd ---
info "Restarting uhttpd..."
/etc/init.d/uhttpd restart

# --- History recording cron job: a tick every 5 minutes. ---
# The tick that finds a record due (config history.record_interval) takes it
# (history.lua maybe_record), so the second of sampling never holds up a
# dashboard request. A tick with nothing due reads one small file. Not every
# minute: crond logs each job it starts, and 1440 lines a day crowd out the
# router's small syslog.
CRON_CMD="*/5 * * * * wget -qO /dev/null http://$LAN_IP:$PORT/api/history/tick # dashboard-history"
info "Adding history cron job (a tick every 5 minutes)..."
update_history_cron "$CRON_CMD"
/etc/init.d/cron restart

# --- Verify ---
sleep 1
HOSTNAME=$(cat /proc/sys/kernel/hostname 2>/dev/null || echo "router")
if wget -qO /dev/null "http://$LAN_IP:$PORT/api/4/load" 2>/dev/null; then
    info "Dashboard is running!"
else
    warn "Dashboard may not have started. Check: logread | grep uhttpd"
fi

info ""
info "  Dashboard: http://$HOSTNAME:$PORT"
info "  Config:    $WWW_CONFIG (edit this one)"
info "  History:   $DASHBOARD_DIR/logs/history.jsonl"
info "  Uninstall: sh $0 --uninstall"
