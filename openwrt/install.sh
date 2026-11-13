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
# Releases are vX.Y.Z tags (README: Versions and updates). A git checkout moves to
# the release's tag; a copy without .git gets the release tarball from GitHub,
# copied over the checkout (config.json, www/ and logs/ are not in a release).
# Then the release's own installer runs.
RELEASE_API="https://api.github.com/repos/mivanit/horatio/releases/latest"
RELEASE_TARBALLS="https://github.com/mivanit/horatio/archive/refs/tags"
SEMVER_RE='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
die() { error "$1"; exit 1; }
# Compare two X.Y.Z[-pre] versions: prints -1, 0 or 1. A pre-release sorts before
# its release.
semver_cmp() {   # <a> <b>
    awk -v a="$1" -v b="$2" 'BEGIN {
        na = split(a, pa, "-"); nb = split(b, pb, "-")
        split(pa[1], x, "."); split(pb[1], y, ".")
        for (i = 1; i <= 3; i++) {
            if (x[i] + 0 < y[i] + 0) { print -1; exit }
            if (x[i] + 0 > y[i] + 0) { print 1; exit }
        }
        print (na > 1 && nb == 1) ? -1 : (na == 1 && nb > 1) ? 1 : 0
    }'
}
update_checkout() {   # [vX.Y.Z]
    ref="$1"
    named=1
    old_version=$(read_version "$DASHBOARD_DIR") || old_version=""
    printf '%s\n' "$old_version" | grep -Eq "$SEMVER_RE" \
        || die "$DASHBOARD_DIR/VERSION is missing or not a version; reinstall from a release."
    use_git=0
    if [ -e "$DASHBOARD_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        use_git=1
        [ -z "$(git -C "$DASHBOARD_DIR" status --porcelain --untracked-files=no)" ] \
            || die "Tracked files have local changes; commit or stash them first."
        branch=$(git -C "$DASHBOARD_DIR" symbolic-ref --quiet --short HEAD) || branch=""
        [ -z "$branch" ] || [ "$branch" = main ] \
            || die "On branch '$branch'; --update only moves main or a release checkout."
        info "Fetching releases..."
        git -C "$DASHBOARD_DIR" fetch --quiet --tags origin || die "git fetch failed."
        if [ -z "$ref" ]; then
            named=0
            ref=$(git -C "$DASHBOARD_DIR" tag -l 'v*' --sort=-v:refname \
                | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1) || ref=""
            [ -n "$ref" ] || die "origin has no releases (vX.Y.Z tags) yet."
        fi
    else
        command -v wget >/dev/null 2>&1 || die "--update needs wget (or git and a git checkout)."
        if [ -z "$ref" ]; then
            named=0
            info "Asking GitHub for the newest release..."
            # Never --no-check-certificate: this download becomes code that runs as root.
            ref=$(wget -qO- "$RELEASE_API" | jsonfilter -e '@.tag_name') || ref=""
            [ -n "$ref" ] || die "Could not get the newest release from GitHub. HTTPS needs a TLS-capable wget and CA certificates (packages libustream-mbedtls and ca-bundle); or name one: --update vX.Y.Z"
        fi
    fi
    case "$ref" in v*) ;; *) ref="v$ref" ;; esac
    if [ "$use_git" = 1 ]; then
        git -C "$DASHBOARD_DIR" rev-parse --quiet --verify "refs/tags/$ref" >/dev/null \
            || die "No release $ref (list them: git tag -l 'v*')."
        new_version=$(git -C "$DASHBOARD_DIR" show "$ref:VERSION" 2>/dev/null | tr -d '[:space:]') || new_version=""
    else
        new_version="${ref#v}"
    fi
    printf '%s\n' "$new_version" | grep -Eq "$SEMVER_RE" \
        || die "$ref is not a release with a VERSION file (it predates --update)."

    case "$(semver_cmp "$new_version" "$old_version")" in
        0)
            if [ "$named" = 0 ] || { [ "$use_git" = 1 ] && [ "$(git -C "$DASHBOARD_DIR" rev-parse "$ref^{commit}")" = "$(git -C "$DASHBOARD_DIR" rev-parse HEAD)" ]; }; then
                info "Already at v$old_version."
                exit 0
            fi ;;
        -1)
            [ "$named" = 1 ] || die "The newest release $ref is older than this checkout's v$old_version. To go back anyway: --update $ref"
            warn "Downgrading v$old_version -> v$new_version. History and config written by v$old_version may not read back." ;;
    esac

    if [ "$use_git" = 1 ]; then
        git -C "$DASHBOARD_DIR" checkout --quiet --detach "$ref" || die "git checkout $ref failed."
    else
        # Unpack beside the checkout: same filesystem, and not in RAM-backed /tmp.
        free_kb=$(df -k "$DASHBOARD_DIR" | awk 'NR == 2 { print $4 }')
        [ "${free_kb:-0}" -ge 4096 ] || die "Less than 4 MB free in $DASHBOARD_DIR."
        work=$(mktemp -d "$DASHBOARD_DIR/.update.XXXXXX") || die "mktemp failed."
        trap 'rm -rf "$work"' EXIT
        trap 'exit 1' HUP INT TERM
        info "Downloading $ref..."
        wget -qO "$work/release.tar.gz" "$RELEASE_TARBALLS/$ref.tar.gz" \
            || die "Download of $ref failed (does the release exist? HTTPS needs libustream-mbedtls and ca-bundle)."
        mkdir "$work/tree"
        # gzip | tar rather than tar -z, which some BusyBox builds lack.
        gzip -dc "$work/release.tar.gz" | tar -xf - -C "$work/tree" || die "Could not unpack $ref."
        # GitHub's archive holds one horatio-X.Y.Z/ directory. Match directories only:
        # a BusyBox tar without pax support unpacks pax_global_header as a file.
        src=""
        for dir in "$work/tree"/*/; do
            [ -z "$src" ] || die "The $ref download has more than one top directory."
            src="${dir%/}"
        done
        [ -d "$src" ] || die "The $ref download is empty."
        [ "$(read_version "$src")" = "$new_version" ] \
            || die "The $ref download does not contain VERSION $new_version; nothing was changed."
        for local_state in config.json www logs .password; do
            [ ! -e "$src/$local_state" ] || die "The $ref download contains $local_state; refusing to overwrite local state."
        done
        cp -R "$src/." "$DASHBOARD_DIR/" \
            || die "Copying $ref over $DASHBOARD_DIR failed partway; rerun: sh $0 --update $ref"
        rm -rf "$work"
        trap - EXIT HUP INT TERM
    fi
    info "Updated v$old_version -> v$new_version. Changes: CHANGELOG.md"
    info "To go back: sh $SCRIPT_DIR/install.sh --update v$old_version"
    info "Running the updated installer..."
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

# --- Web root ---
# Refresh the served copies; config.json in www/ is never touched here.
info "Copying frontend into $WWW_DIR..."
mkdir -p "$WWW_DIR"
rm -rf "$WWW_DIR/static" "$WWW_DIR/index.html"
# uhttpd sends no Cache-Control for files, so browsers may keep old scripts for
# days after an update: give the asset URLs this install's version and time.
ASSET_STAMP="$(read_version "$DASHBOARD_DIR" || true)-$(date +%s)"
sed -E "s#\"(/static/[^\"?]+\.(js|css))\"#\"\1?v=$ASSET_STAMP\"#g" \
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
info "  Config:    $WWW_CONFIG (edit this one)"
info "  History:   $DASHBOARD_DIR/logs/history.jsonl"
info "  Uninstall: sh $0 --uninstall"
