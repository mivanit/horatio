#!/bin/sh
# Shared frontend/config preparation. Source from either Lua installer after
# setting DASHBOARD_DIR, WWW_DIR, CONFIG, WWW_CONFIG and the logging helpers.

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

