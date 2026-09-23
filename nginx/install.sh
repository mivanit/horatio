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
if [ "$2" = "--uninstall" ]; then
    echo "Removing nginx config for $DOMAIN..."
    rm -f "$CONF_DEST"
    rm -rf "$INDEX_DIR"

    if [ "$PLATFORM" = "openwrt" ]; then
        echo "Restoring uhttpd to port 80..."
        uci set uhttpd.main.listen_http='0.0.0.0:80'
        uci set uhttpd.main.listen_https='0.0.0.0:443' 2>/dev/null || true
        uci commit uhttpd
        /etc/init.d/uhttpd restart
        /etc/init.d/nginx restart 2>/dev/null || true
    else
        nginx -t 2>/dev/null && systemctl reload-or-restart nginx 2>/dev/null || true
        echo "Note: nginx default site was not restored."
        echo "  To restore: ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default"
    fi

    echo "Done."
    exit 0
fi

# --- Install nginx if needed ---
if ! command -v nginx >/dev/null 2>&1; then
    echo "Installing nginx..."
    if [ "$PLATFORM" = "ubuntu" ]; then
        apt install -y nginx
    elif command -v opkg >/dev/null 2>&1; then
        opkg update && opkg install nginx
    fi
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

# --- Write files ---
mkdir -p "$INDEX_DIR"
printf '%s\n' "$INDEX" > "$INDEX_DIR/index.html"

if [ "$PLATFORM" = "ubuntu" ]; then
    rm -f /etc/nginx/sites-enabled/default
fi

if [ "$PLATFORM" = "openwrt" ]; then
    echo "Moving uhttpd (LuCI) to port 8081..."
    uci set uhttpd.main.listen_http='127.0.0.1:8081'
    uci -q delete uhttpd.main.listen_https 2>/dev/null || true
    uci commit uhttpd
    /etc/init.d/uhttpd restart
    mkdir -p /etc/nginx/conf.d
fi

# Write conf and test (if nginx -t fails, set -e exits but nginx keeps old running config)
printf '%s\n' "$CONF" > "$CONF_DEST"

echo "Testing nginx config..."
if [ "$PLATFORM" = "openwrt" ]; then
    nginx -t -c /etc/nginx/uci.conf
else
    nginx -t
fi

echo "Reloading nginx..."
if [ "$PLATFORM" = "ubuntu" ]; then
    systemctl reload-or-restart nginx
else
    /etc/init.d/nginx enable
    /etc/init.d/nginx restart
fi

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
