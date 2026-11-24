#!/bin/sh
# Install the Lua backend as an unprivileged systemd service on Linux.
# Run as the account that owns the checkout; sudo is used only for systemd.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
DASHBOARD_DIR=$(dirname "$SCRIPT_DIR")
WWW_DIR="$DASHBOARD_DIR/www"
CONFIG="$DASHBOARD_DIR/config.json"
WWW_CONFIG="$WWW_DIR/config.json"
RUNTIME="$DASHBOARD_DIR/.lua-runtime"

info() { printf '[+] %s\n' "$1"; }
warn() { printf '[!] %s\n' "$1"; }
error() { printf '[-] %s\n' "$1" >&2; }
die() { error "$1"; exit 1; }
read_version() { [ -f "$1/VERSION" ] && tr -d '[:space:]' < "$1/VERSION"; }

case "${1:-}" in
    -h|--help)
        cat <<'EOF'
Usage: sh lua/install-linux.sh              install or refresh the Lua service
       sh lua/install-linux.sh --uninstall  remove its services; keep config/history

Requires systemd, sudo, wget, and the build dependencies documented in lua/README.md#linux.
Run as your own user. Native dependencies are built privately in .lua-runtime/.
Update with git pull, then run this installer again.

LISTEN_ADDRESS=127.0.0.1 PORT=8080 are the defaults. To allow LAN connections:
  LISTEN_ADDRESS=192.168.1.10 sh lua/install-linux.sh
Supply the same overrides when reinstalling. The HTTP service has no authentication;
use a trusted network or an authenticated reverse proxy.
EOF
        exit 0 ;;
    ''|--uninstall) ;;
    *) die "Unknown option: $1 (see --help)." ;;
esac
[ "$#" -le 1 ] || die "Too many arguments (see --help)."
[ "$(id -u)" -ne 0 ] || die "Run this installer as your own user, not root or sudo."
# These values are embedded in systemd units: reject its escaping/specifier syntax.
case "$DASHBOARD_DIR" in
    *[!A-Za-z0-9_./-]*) die "Checkout path must contain only letters, digits, _, ., / or -." ;;
esac
SERVICE_USER=$(id -un)
SERVICE_GROUP=$(id -gn)
case "$SERVICE_USER:$SERVICE_GROUP" in
    *[!A-Za-z0-9_:.-]*) die "Unsupported service user or group name." ;;
esac
for command in systemctl sudo; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required. This installer needs systemd Linux."
done

if [ "${1:-}" = --uninstall ]; then
    for unit in horatio-lua-history.timer horatio-lua.service; do
        if [ -f "/etc/systemd/system/$unit" ]; then
            sudo systemctl disable --now "$unit"
        fi
    done
    if [ -f /etc/systemd/system/horatio-lua-history.service ]; then
        sudo systemctl stop horatio-lua-history.service
    fi
    sudo rm -f /etc/systemd/system/horatio-lua.service \
        /etc/systemd/system/horatio-lua-history.service /etc/systemd/system/horatio-lua-history.timer
    sudo systemctl daemon-reload
    info "Lua service removed. Checkout, native runtime, config and history kept."
    exit 0
fi

LISTEN_ADDRESS=${LISTEN_ADDRESS:-127.0.0.1}
PORT=${PORT:-8080}
# Deliberately accept IPv4 only; addresses are also used in the history tick URL.
printf '%s\n' "$LISTEN_ADDRESS" | awk -F. '
    NF != 4 { exit 1 }
    { for (i=1; i<=4; i++) if ($i !~ /^[0-9]+$/ || length($i)>3 || $i>255) exit 1 }
' || die "LISTEN_ADDRESS must be an IPv4 address."
case "$PORT" in ''|*[!0-9]*) die "PORT must be between 1024 and 65535." ;; esac
[ "${#PORT}" -le 5 ] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] \
    || die "PORT must be between 1024 and 65535."
for command in wget sha256sum; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required."
done

# Rebuild when the pinned runtime recipe changes, keeping dependencies local.
recipe=$(sha256sum "$DASHBOARD_DIR/scripts/setup-lua-runtime.sh" | cut -d ' ' -f 1)
if [ ! -x "$RUNTIME/bin/uhttpd" ] || [ "$(cat "$RUNTIME/.recipe" 2>/dev/null || true)" != "$recipe" ]; then
    sh "$DASHBOARD_DIR/scripts/setup-lua-runtime.sh" "$RUNTIME"
    printf '%s\n' "$recipe" > "$RUNTIME/.recipe"
fi

. "$SCRIPT_DIR/prepare-web.sh"
mkdir -p "$DASHBOARD_DIR/logs"
[ -w "$DASHBOARD_DIR/logs" ] || die "The service account must be able to write logs/."
TICK_ADDRESS=$LISTEN_ADDRESS
[ "$TICK_ADDRESS" != 0.0.0.0 ] || TICK_ADDRESS=127.0.0.1

unit_dir=$(mktemp -d)
trap 'rm -rf "$unit_dir"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$unit_dir/horatio-lua.service" <<EOF
[Unit]
Description=Horatio Lua dashboard
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$DASHBOARD_DIR
ExecStart=$RUNTIME/bin/uhttpd -f -p $LISTEN_ADDRESS:$PORT -h $WWW_DIR -L $SCRIPT_DIR/handler.lua -l /api -D -S -n 50
Restart=on-failure
RestartSec=3
PrivateTmp=true
ProtectSystem=strict
ProtectHome=tmpfs
BindReadOnlyPaths=$DASHBOARD_DIR
BindPaths=$DASHBOARD_DIR/logs
ProtectControlGroups=true
# Permit the system ping binary's file capability; the server gets none itself.
CapabilityBoundingSet=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF
cat > "$unit_dir/horatio-lua-history.service" <<EOF
[Unit]
Description=Record Horatio Lua history
Requires=horatio-lua.service
After=horatio-lua.service

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
ExecStart=$(command -v wget) -q -T 30 -t 1 -O /dev/null http://$TICK_ADDRESS:$PORT/api/history/tick
EOF
cat > "$unit_dir/horatio-lua-history.timer" <<'EOF'
[Unit]
Description=Sample Horatio Lua history every five minutes

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
sudo install -m 644 "$unit_dir/horatio-lua.service" \
    "$unit_dir/horatio-lua-history.service" "$unit_dir/horatio-lua-history.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable horatio-lua.service horatio-lua-history.timer
sudo systemctl restart horatio-lua.service horatio-lua-history.timer
# Catch bad libraries, a conflicting port or a handler load failure immediately.
attempt=0
until sudo systemctl is-active --quiet horatio-lua.service \
    && wget -q -T 2 -t 1 -O /dev/null "http://$TICK_ADDRESS:$PORT/api/4/load"; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 5 ] || die "Lua service failed its readiness check. See: journalctl -u horatio-lua.service"
    sleep 1
done
info "Lua dashboard: http://$LISTEN_ADDRESS:$PORT"
info "Config: $WWW_CONFIG; restart horatio-lua.service after editing."
info "History: $DASHBOARD_DIR/logs/history.jsonl"
