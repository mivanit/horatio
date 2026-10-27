#!/bin/sh
# OpenWrt dashboard installer
# Configures a uhttpd instance on port 8080 (LAN only) to serve
# the dashboard with Lua API handler.
#
# Usage:
#   sh install.sh              # install and start
#   sh install.sh --uninstall  # remove dashboard config

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UCI_SECTION="dashboard"
PORT=8080

info()  { printf "[+] %s\n" "$1"; }
warn()  { printf "[!] %s\n" "$1"; }
error() { printf "[-] %s\n" "$1"; }

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
    info "Dashboard removed."
    exit 0
fi

# --- Help ---
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat <<EOF
Usage: sh install.sh              # install / update dashboard
       sh install.sh --uninstall  # remove dashboard config

Configures a uhttpd instance on port $PORT (LAN only) serving
the dashboard with Lua API handler.

Override LAN IP:  LAN_IP=192.168.1.1 sh install.sh
EOF
    exit 0
fi

# --- Preflight ---
command -v uci    >/dev/null 2>&1 || { error "uci not found. Requires OpenWrt."; exit 1; }
command -v uhttpd >/dev/null 2>&1 || { error "uhttpd not found."; exit 1; }

# --- Dependencies ---
info "Checking dependencies..."
if command -v apk >/dev/null 2>&1; then
    apk add uhttpd-mod-lua luci-lib-jsonc luci-lib-nixio
elif command -v opkg >/dev/null 2>&1; then
    opkg list-installed | grep -q uhttpd-mod-lua || opkg install uhttpd-mod-lua
    opkg list-installed | grep -q luci-lib-jsonc || opkg install luci-lib-jsonc
    opkg list-installed | grep -q luci-lib-nixio || opkg install luci-lib-nixio
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
fi
if [ -z "$LAN_IP" ]; then
    error "Could not detect LAN IP. Set it manually: LAN_IP=192.168.1.1 sh $0"
    exit 1
fi
info "Binding to $LAN_IP:$PORT"

# Config must exist before uhttpd loads the Lua handler at startup. Preserve
# the machine's existing config on updates, as the Linux installer does.
if [ ! -f "$DASHBOARD_DIR/config.json" ]; then
    cp "$DASHBOARD_DIR/config.example.json" "$DASHBOARD_DIR/config.json"
    info "Created config.json from config.example.json: edit it (title, ping targets, thresholds)."
fi

# --- Configure uhttpd ---
info "Configuring uhttpd dashboard instance..."
uci delete "uhttpd.$UCI_SECTION" 2>/dev/null || true

uci set "uhttpd.$UCI_SECTION=uhttpd"
uci add_list "uhttpd.$UCI_SECTION.listen_http=$LAN_IP:$PORT"
uci add_list "uhttpd.$UCI_SECTION.listen_http=127.0.0.1:$PORT"
uci set "uhttpd.$UCI_SECTION.home=$DASHBOARD_DIR"
uci set "uhttpd.$UCI_SECTION.lua_prefix=/api"
uci set "uhttpd.$UCI_SECTION.lua_handler=$SCRIPT_DIR/handler.lua"
uci set "uhttpd.$UCI_SECTION.max_requests=50"
uci set "uhttpd.$UCI_SECTION.no_symlinks=0"
uci set "uhttpd.$UCI_SECTION.no_dirlists=1"

uci commit uhttpd

# --- Restart uhttpd ---
info "Restarting uhttpd..."
/etc/init.d/uhttpd restart

# --- History recording cron job: twice daily floor (03:00 and 15:00). ---
# Records also piggyback on API requests (history.lua maybe_record), so this
# only matters when nobody has the dashboard open.
CRON_CMD="0 3,15 * * * wget -qO /dev/null --post-data='' http://$LAN_IP:$PORT/api/history/record # dashboard-history"
info "Adding history cron job (03:00 and 15:00)..."
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
info "  Config:    $DASHBOARD_DIR/config.json"
info "  History:   $DASHBOARD_DIR/logs/history.jsonl"
info "  Uninstall: sh $0 --uninstall"
