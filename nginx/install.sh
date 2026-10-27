#!/bin/sh
# Nginx reverse proxy installer
# Reads a .services file and generates nginx config + index page.
# Works on both Ubuntu (systemd) and OpenWrt (uci/init.d).
#
# Usage:
#   install.sh <services-file>              # install or update
#   install.sh <services-file> --uninstall  # remove
#   install.sh --help                       # show help

set -e

# --- Help ---
if [ "$1" = "--help" ] || [ "$1" = "-h" ] || [ -z "$1" ]; then
    cat <<'EOF'
Usage: install.sh <services-file> [--uninstall]

Generates an nginx reverse proxy config and index page from a .services file.

Services file format:
  # domain: example.internal
  dashboard 8080
  myapp 9000

Each line is: <name> <port>
The domain is read from the "# domain:" header line.

Examples:
  sudo sh install.sh example.services        # install/update on Ubuntu
  sh install.sh myrouter.services               # install/update on OpenWrt
  sudo sh install.sh example.services --uninstall

The generated config maps:
  <name>.<domain> -> localhost:<port>
And serves an index page with links at the bare domain.
EOF
    exit 0
fi

# --- Args ---
SERVICES_FILE="$1"
[ -f "$SERVICES_FILE" ] || { echo "Error: file not found: $SERVICES_FILE"; exit 1; }

# Resolve to absolute path
SERVICES_FILE="$(cd "$(dirname "$SERVICES_FILE")" && pwd)/$(basename "$SERVICES_FILE")"

# --- Parse services file ---
DOMAIN=$(grep '^# domain:' "$SERVICES_FILE" | head -1 | sed 's/^# domain: *//; s/ *$//')
[ -n "$DOMAIN" ] || { echo "Error: no '# domain:' header in $SERVICES_FILE"; exit 1; }

# Sanitize domain for use as filename
CONF_NAME=$(echo "$DOMAIN" | tr '.' '-')

# --- Platform detection ---
if command -v systemctl >/dev/null 2>&1; then
    PLATFORM="ubuntu"
    CONF_DEST="/etc/nginx/sites-enabled/$CONF_NAME"
    INDEX_DIR="/var/www/$CONF_NAME"
elif command -v uci >/dev/null 2>&1; then
    PLATFORM="openwrt"
    CONF_DEST="/etc/nginx/conf.d/$CONF_NAME.conf"
    INDEX_DIR="/www/$CONF_NAME"
else
    echo "Error: unsupported platform (no systemctl or uci found)"
    exit 1
fi

# --- Uninstall ---
if [ "$2" = "--uninstall" ] && [ "$PLATFORM" = ubuntu ]; then
    echo "Removing nginx config for $DOMAIN..."
    rm -f "$CONF_DEST"
    rm -rf "$INDEX_DIR"

    nginx -t 2>/dev/null && systemctl reload-or-restart nginx 2>/dev/null || true
    echo "Note: nginx default site was not restored."
    echo "  To restore: ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default"

    echo "Done."
    exit 0
fi

# Automatic OpenWrt removal stops nginx altogether: its packaged defaults also
# claim ports 80/443. Only do that for the dedicated, stock UCI setup. Unknown
# configurations need a manual port handoff, not an nginx-language parser here.
stock_openwrt_nginx() {
    case "$(uci -q get nginx.global.uci_enable)" in 1|on|true|yes|enabled) ;; *) return 1 ;; esac
    [ "$(uci -q get nginx.global)" = main ] && [ "$(uci -q get nginx._lan)" = server ] &&
        [ "$(uci -q get nginx._redirect2ssl)" = server ] || return 1
    keys=$(uci -q show nginx) || return 1
    if printf '%s\n' "$keys" | sed 's/=.*//' | grep -Ev '^nginx\.(global(\.uci_enable)?|_lan(\.(listen|server_name|include|uci_manage_ssl|ssl_certificate|ssl_certificate_key|ssl_session_cache|ssl_session_timeout|access_log))?|_redirect2ssl(\.(listen|server_name|return))?)$' >/dev/null; then return 1; fi
    [ "$(uci -q get nginx._lan.server_name)" = _lan ] &&
        [ "$(uci -q get nginx._redirect2ssl.server_name)" = _redirect2ssl ] &&
        [ "$(uci -q get nginx._lan.include)" = 'restrict_locally conf.d/*.locations' ] &&
        [ "$(uci -q get nginx._redirect2ssl.return)" = '302 https://$host$request_uri' ] || return 1
    case "$(uci -q get nginx._lan.listen)" in '443 ssl default_server'|'443 ssl default_server [::]:443 ssl default_server') ;; *) return 1 ;; esac
    case "$(uci -q get nginx._redirect2ssl.listen)" in '80'|'80 [::]:80') ;; *) return 1 ;; esac
    for file in /etc/nginx/conf.d/*.conf /etc/nginx/conf.d/*.locations; do
        [ "$file" = "$CONF_DEST" ] || [ ! -e "$file" ] || return 1
    done
    # The upstream nginx-util 1.2 template, ignoring comments/whitespace. This
    # also excludes custom sites/includes added directly to the template.
    expected='worker_processes auto; user root; include module.d/*.module; events {}
http {
access_log off;
log_format openwrt
  '\''$request_method $scheme://$host$request_uri => $status'\''
  '\'' (${body_bytes_sent}B in ${request_time}s) <- $http_referer'\'';
include mime.types; default_type application/octet-stream; sendfile on;
client_max_body_size 128M; large_client_header_buffers 2 1k;
gzip on; gzip_vary on; gzip_proxied any;
root /www;
include conf.d/*.conf;
}'
    [ -f /etc/nginx/uci.conf.template ] || return 1
    actual=$(sed '/^[[:space:]]*#/d' /etc/nginx/uci.conf.template | tr -d '[:space:]')
    [ "$actual" = "$(printf '%s' "$expected" | tr -d '[:space:]')" ] || return 1
    # Included support files must not hide additional sites either.
    for file in /etc/nginx/module.d/*.module; do
        [ -e "$file" ] || continue
        if sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$file" | grep -Ev '^[[:space:]]*load_module[[:space:]]+[^;]+;[[:space:]]*$' >/dev/null; then return 1; fi
    done
    [ -f /etc/nginx/restrict_locally ] || return 1
    if sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /etc/nginx/restrict_locally | grep -Ev '^[[:space:]]*(allow|deny)[[:space:]]+[^;]+;[[:space:]]*$' >/dev/null; then return 1; fi
}

if [ "$2" = --uninstall ]; then
    stock_openwrt_nginx || {
        echo "Error: automatic removal requires a dedicated stock OpenWrt nginx setup." >&2
        echo "Custom/manual sites or an unrecognized template were found; no files or services were changed." >&2
        echo "Arrange a manual port 80/443 handoff to uhttpd; see nginx/README.md." >&2
        exit 1
    }
fi

# --- Install nginx if needed ---
if [ "$2" != --uninstall ] && ! command -v nginx >/dev/null 2>&1; then
    echo "Installing nginx..."
    if [ "$PLATFORM" = "ubuntu" ]; then
        apt install -y nginx
    elif command -v apk >/dev/null 2>&1; then
        apk add nginx
    elif command -v opkg >/dev/null 2>&1; then
        opkg update && opkg install nginx
    fi
fi
command -v nginx >/dev/null 2>&1 || {
    echo "Error: nginx is unavailable; no service configuration was changed." >&2
    exit 1
}
if [ "$PLATFORM" = openwrt ]; then
    for required in wget timeout; do
        command -v "$required" >/dev/null 2>&1 || {
            echo "Error: $required is required to verify nginx startup; no service configuration was changed." >&2
            exit 1
        }
    done
fi

# --- Generate nginx conf ---
echo "Generating nginx config for $DOMAIN..."

CONF=""

# Default server block: index page
CONF="${CONF}# Auto-generated from $(basename "$SERVICES_FILE")
# Do not edit -- re-run install.sh to update

server_names_hash_bucket_size 64;

server {
    listen 80;
    server_name $DOMAIN;
    root $INDEX_DIR;
    index index.html;
}
"

# Service server blocks
while IFS= read -r line || [ -n "$line" ]; do
    # Skip comments and blank lines (but not the domain directive)
    case "$line" in
        '#'*|'') continue ;;
    esac

    name=$(echo "$line" | awk '{print $1}')
    port=$(echo "$line" | awk '{print $2}')
    [ -n "$name" ] && [ -n "$port" ] || continue

    CONF="${CONF}
server {
    listen 80;
    server_name ${name}.${DOMAIN};
    location / {
        proxy_pass http://127.0.0.1:${port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
        add_header X-Content-Type-Options \"nosniff\" always;
        add_header X-Frame-Options \"SAMEORIGIN\" always;
        add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;
    }
}
"
done < "$SERVICES_FILE"

# --- Generate index.html ---
echo "Generating index page..."

LINKS=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        '#'*|'') continue ;;
    esac
    name=$(echo "$line" | awk '{print $1}')
    port=$(echo "$line" | awk '{print $2}')
    [ -n "$name" ] && [ -n "$port" ] || continue
    LINKS="${LINKS}    <li><a href=\"http://${name}.${DOMAIN}\">${name}</a></li>
"
done < "$SERVICES_FILE"

INDEX="<!DOCTYPE html>
<html>
<head>
<meta charset=\"utf-8\">
<title>${DOMAIN}</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; }
a { color: #0066cc; text-decoration: none; }
a:hover { text-decoration: underline; }
li { margin: 0.5em 0; }
</style>
</head>
<body>
<h1>${DOMAIN}</h1>
<ul>
${LINKS}</ul>
</body>
</html>"

# --- Publish with recovery until validation and service reload both succeed ---
# Backups stay OUTSIDE nginx's include directories: sites-enabled/* would load
# a backup as another live configuration. -P keeps symlinks as directory entries
# instead of copying or later overwriting their targets.
mkdir -p "$(dirname "$CONF_DEST")"
RECOVERY_DIR="$(mktemp -d /etc/nginx/.horatio-XXXXXX)"
CONFIG_CHANGED=0
DEFAULT_CHANGED=0
INDEX_CHANGED=0
UHTTPD_CHANGED=0
NGINX_CHANGED=0
COMMITTED=0
INDEX_TMP=""
NGINX_WAS_RUNNING=0
NGINX_WAS_ENABLED=0
NGINX_ENABLE_CHANGED=0
LUCI_PROBE=""

save_entry() {
    [ ! -d "$1" ] || { echo "Error: expected a file, found a directory: $1" >&2; return 1; }
    if [ -e "$1" ] || [ -L "$1" ]; then cp -pP "$1" "$2"; fi
}

restore_entry() {
    if [ -e "$2" ] || [ -L "$2" ]; then
        # Keep the backup until EVERY restore, including service restart, works.
        cp -pP "$2" "$2.restore" && mv -f "$2.restore" "$1"
    else
        rm -f "$1"
    fi
}

# procd's start/restart returns before the daemon has bound its listeners.
# On rollback the old service may have arbitrary URLs, so require four seconds
# of sustained running state (nginx retries failed binds for about 2.5 seconds).
wait_openwrt_service() (
    attempts=0
    stable=0
    while [ "$attempts" -lt 15 ]; do
        sleep 1
        if "/etc/init.d/$1" running >/dev/null 2>&1; then
            stable=$((stable + 1))
            [ "$stable" -lt 4 ] || exit 0
        else
            stable=0
        fi
        attempts=$((attempts + 1))
    done
    echo "Error: $1 did not stay running after startup." >&2
    exit 1
)

stop_openwrt_nginx() (
    /etc/init.d/nginx stop || exit 1
    attempts=0
    while [ "$attempts" -lt 15 ]; do
        /etc/init.d/nginx running >/dev/null 2>&1 || exit 0
        sleep 1
        attempts=$((attempts + 1))
    done
    echo "Error: nginx did not stop." >&2
    exit 1
)

wait_openwrt_index() (
    attempts=0
    while [ "$attempts" -lt 15 ]; do
        # Use loopback and an explicit Host header: LAN DNS need not be ready,
        # and an unrelated service on port 80 must not count as our nginx.
        if /etc/init.d/nginx running >/dev/null 2>&1 \
            && body=$(timeout -s KILL 3 wget -qO- -T 2 -Y off --header="Host: $DOMAIN" http://127.0.0.1/ 2>/dev/null) \
            && [ "$body" = "$INDEX" ]; then
            exit 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    echo "Error: nginx did not serve the generated index after startup." >&2
    exit 1
)

wait_openwrt_luci() (
    attempts=0
    while [ "$attempts" -lt 15 ]; do
        if /etc/init.d/uhttpd running >/dev/null 2>&1 \
            && body=$(timeout -s KILL 3 wget -qO- -T 2 -Y off --no-check-certificate "http://127.0.0.1/$(basename "$LUCI_PROBE")" 2>/dev/null) \
            && [ "$body" = "$LUCI_TOKEN" ]; then
            exit 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    echo "Error: uhttpd did not serve its verification file after restoring port 80." >&2
    exit 1
)

finish() {
    result=$?
    trap - 0 HUP INT TERM
    [ -z "$LUCI_PROBE" ] || rm -f "$LUCI_PROBE" || true
    if [ "$COMMITTED" = 0 ]; then
        restored=1
        # A failed first install must release port 80 before LuCI returns to it.
        if [ "$PLATFORM" = openwrt ] && [ "$NGINX_CHANGED" = 1 ]; then
            stop_openwrt_nginx || restored=0
        fi
        if [ "$CONFIG_CHANGED" = 1 ]; then
            restore_entry "$CONF_DEST" "$RECOVERY_DIR/config" || restored=0
        fi
        if [ "$DEFAULT_CHANGED" = 1 ]; then
            restore_entry /etc/nginx/sites-enabled/default "$RECOVERY_DIR/default" || restored=0
        fi
        if [ "$INDEX_CHANGED" = 1 ]; then
            restore_entry "$INDEX_DIR/index.html" "$RECOVERY_DIR/index" || restored=0
        fi
        if [ "$UHTTPD_CHANGED" = 1 ]; then
            if uci import uhttpd < "$RECOVERY_DIR/uhttpd" && uci commit uhttpd; then
                /etc/init.d/uhttpd restart && wait_openwrt_service uhttpd || restored=0
            else
                restored=0
            fi
        fi
        if [ "$NGINX_ENABLE_CHANGED" = 1 ]; then
            if [ "$NGINX_WAS_ENABLED" = 1 ]; then
                /etc/init.d/nginx enable || restored=0
            else
                /etc/init.d/nginx disable || restored=0
            fi
        fi
        if [ "$NGINX_CHANGED" = 1 ]; then
            if [ "$PLATFORM" = ubuntu ]; then
                if [ "$NGINX_WAS_RUNNING" = 1 ]; then
                    systemctl reload-or-restart nginx || restored=0
                else
                    systemctl stop nginx || restored=0
                fi
            elif [ "$NGINX_WAS_RUNNING" = 1 ]; then
                /etc/init.d/nginx start && wait_openwrt_service nginx || restored=0
            fi
        fi
        if [ "$restored" = 0 ]; then
            echo "Error: recovery was incomplete; retained recovery files in $RECOVERY_DIR" >&2
            exit 1
        fi
    fi
    [ -z "$INDEX_TMP" ] || rm -f "$INDEX_TMP"
    rm -rf "$RECOVERY_DIR"
    exit "$result"
}
trap finish 0
trap 'exit 1' HUP INT TERM

save_entry "$CONF_DEST" "$RECOVERY_DIR/config"
if [ "$PLATFORM" = ubuntu ]; then
    if [ "$CONF_DEST" != /etc/nginx/sites-enabled/default ]; then
        save_entry /etc/nginx/sites-enabled/default "$RECOVERY_DIR/default"
    fi
    if systemctl is-active --quiet nginx; then NGINX_WAS_RUNNING=1; fi
else
    if /etc/init.d/nginx running; then NGINX_WAS_RUNNING=1; fi
fi

if [ "$2" = --uninstall ]; then
    save_entry "$INDEX_DIR/index.html" "$RECOVERY_DIR/index"
    uci export uhttpd > "$RECOVERY_DIR/uhttpd"
    if /etc/init.d/nginx enabled; then NGINX_WAS_ENABLED=1; fi
    LUCI_HOME=$(uci -q get uhttpd.main.home)
    [ -d "$LUCI_HOME" ] || { echo "Error: uhttpd.main.home is not a directory." >&2; exit 1; }
    LUCI_PROBE=$(mktemp "$LUCI_HOME/horatio-check-XXXXXX")
    LUCI_TOKEN=$(basename "$LUCI_PROBE")
    printf '%s\n' "$LUCI_TOKEN" > "$LUCI_PROBE"
    chmod 644 "$LUCI_PROBE"

    echo "Releasing nginx listeners and restoring uhttpd..."
    NGINX_CHANGED=1
    stop_openwrt_nginx
    NGINX_ENABLE_CHANGED=1
    /etc/init.d/nginx disable
    UHTTPD_CHANGED=1
    uci set uhttpd.main.listen_http='0.0.0.0:80'
    uci set uhttpd.main.listen_https='0.0.0.0:443'
    uci commit uhttpd
    /etc/init.d/uhttpd restart
    wait_openwrt_luci

    # Keep both generated files available for rollback until LuCI is ready.
    CONFIG_CHANGED=1
    rm -f "$CONF_DEST"
    INDEX_CHANGED=1
    rm -f "$INDEX_DIR/index.html"
    COMMITTED=1
    rmdir "$INDEX_DIR" 2>/dev/null || true  # preserve any other user content
    echo "Done. LuCI is restored; nginx is stopped and disabled."
    exit 0
fi

printf '%s\n' "$CONF" > "$RECOVERY_DIR/new-config"
chmod 644 "$RECOVERY_DIR/new-config"
CONFIG_CHANGED=1
mv -f "$RECOVERY_DIR/new-config" "$CONF_DEST"
if [ "$PLATFORM" = ubuntu ] && [ "$CONF_DEST" != /etc/nginx/sites-enabled/default ]; then
    DEFAULT_CHANGED=1
    rm -f /etc/nginx/sites-enabled/default
fi

echo "Testing nginx config..."
if [ "$PLATFORM" = "openwrt" ]; then
    # Like OpenWrt's nginx_init, prepare RAM-backed config without starting the
    # service. uci.conf can be a dangling symlink after a disabled-service boot.
    case "$(uci -q get nginx.global.uci_enable 2>/dev/null || true)" in
        1|on|true|yes|enabled) OPENWRT_NGINX_CONF=/etc/nginx/uci.conf ;;
        *) OPENWRT_NGINX_CONF=/etc/nginx/nginx.conf ;;
    esac
    mkdir -p /var/log/nginx /var/lib/nginx
    if command -v nginx-util >/dev/null 2>&1 && ! nginx-util init_lan; then
        # The native init script also permits manual config after helper failure.
        # UCI mode needs freshly generated configuration, never a stale fallback.
        [ "$OPENWRT_NGINX_CONF" != /etc/nginx/uci.conf ] || exit 1
        echo "Warning: nginx runtime preparation failed; validating manual configuration." >&2
    fi
    # Select manual mode explicitly even if a stale generated file still exists.
    nginx -t -c "$OPENWRT_NGINX_CONF"
else
    nginx -t
fi

# The candidate is valid. Only now replace the index and change LuCI's listener.
mkdir -p "$INDEX_DIR"
save_entry "$INDEX_DIR/index.html" "$RECOVERY_DIR/index"
INDEX_TMP="$(mktemp "$INDEX_DIR/.index-XXXXXX")"
printf '%s\n' "$INDEX" > "$INDEX_TMP"
chmod 644 "$INDEX_TMP"
INDEX_CHANGED=1
mv -f "$INDEX_TMP" "$INDEX_DIR/index.html"
INDEX_TMP=""
if [ "$PLATFORM" = openwrt ]; then
    uci export uhttpd > "$RECOVERY_DIR/uhttpd"
    echo "Moving uhttpd (LuCI) to port 8081..."
    UHTTPD_CHANGED=1
    uci set uhttpd.main.listen_http='127.0.0.1:8081'
    uci -q delete uhttpd.main.listen_https 2>/dev/null || true
    uci commit uhttpd
    /etc/init.d/uhttpd restart
    wait_openwrt_service uhttpd
fi

echo "Reloading nginx..."
NGINX_CHANGED=1
if [ "$PLATFORM" = "ubuntu" ]; then
    systemctl reload-or-restart nginx
else
    # Wait for old workers to leave, so they cannot satisfy the HTTP probe by
    # serving the newly replaced index while the replacement daemon fails.
    stop_openwrt_nginx
    /etc/init.d/nginx start
    wait_openwrt_index
    /etc/init.d/nginx enable
fi
COMMITTED=1

echo ""
echo "Reverse proxy is active for $DOMAIN"
echo "  Index:     http://$DOMAIN"

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        '#'*|'') continue ;;
    esac
    name=$(echo "$line" | awk '{print $1}')
    port=$(echo "$line" | awk '{print $2}')
    [ -n "$name" ] && [ -n "$port" ] || continue
    printf "  %-10s http://%s.%s -> :%s\n" "$name" "$name" "$DOMAIN" "$port"
done < "$SERVICES_FILE"

echo ""
echo "  Config:    $CONF_DEST"
echo "  Uninstall: sh $0 $1 --uninstall"
