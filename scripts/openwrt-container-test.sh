#!/usr/bin/env bash
# Install, then update, the dashboard on real OpenWrt: the official rootfs image
# booted with its own init (procd), uhttpd and package feeds. CI runs it on every
# push (.github/workflows/check.yml); locally:
#   scripts/openwrt-container-test.sh fresh    # docker; CONTAINER=podman to switch
#   scripts/openwrt-container-test.sh migrate  # v0.2.1 -> checkout, with LuCI
# OPENWRT_IMAGE picks another image. Needs network: the package feeds, Chart.js,
# and GitHub for the update.
#
# 1. The checkout's tracked files (no .git, as a copied-over router has them) are
#    installed fresh, and the dashboard must answer with this checkout's VERSION.
# 2. In the fresh scenario, once there is a release: the box's
#    VERSION is set to 0.0.0 and `--update` must download and install that
#    release, keeping www/config.json.
# 3. The migrate scenario installs v0.2.1 with LuCI, records history, then copies
#    the candidate over it and runs the renamed installer (the documented manual
#    migration). Both scenarios get a new container. No host services change.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${CONTAINER:-docker}"
IMAGE="${OPENWRT_IMAGE:-docker.io/openwrt/rootfs:x86-64-openwrt-25.12}"
NAME="horatio-openwrt-test-$$"
REPO="mivanit/horatio"
DIR=/root/horatio
SCENARIO=${1:-fresh}
BASELINE=v0.2.1
case "$SCENARIO" in fresh|migrate) ;; *) echo "usage: $0 [fresh|migrate]" >&2; exit 2 ;; esac
SNAPSHOT=$(mktemp -d)

fail() { echo "FAIL: $*" >&2; exit 1; }
on_router() { "$ENGINE" exec "$NAME" sh -c "$1"; }
cleanup() {
    if [ "${1:-0}" != 0 ]; then
        echo "--- router log (uhttpd) ---" >&2
        on_router 'logread | grep -i uhttpd | tail -20' >&2 || true
    fi
    "$ENGINE" rm -f "$NAME" > /dev/null 2>&1 || true
    rm -rf "$SNAPSHOT"
}
trap 'cleanup $?' EXIT

echo "== Booting $IMAGE"
"$ENGINE" run -d --name "$NAME" "$IMAGE" /sbin/init > /dev/null
for _ in $(seq 60); do
    on_router 'ubus list network.interface > /dev/null 2>&1' && break
    sleep 1
done
on_router 'ubus list network.interface > /dev/null' || fail "procd/netifd did not come up"
# The container's own address (OpenWrt leaves the engine's interface alone).
IP="$(on_router "ip -4 addr | awk '/inet / && !/ 127\\./ { sub(/\\/.*/, \"\", \$2); print \$2; exit }'")"
[ -n "$IP" ] || fail "no IPv4 address in the container"
echo "   address $IP"

# GET <path> from the dashboard, retrying while uhttpd starts.
fetch() {
    local out
    for _ in $(seq 15); do
        out="$(on_router "wget -qO- http://$IP:8080$1" 2> /dev/null)" && { printf '%s' "$out"; return 0; }
        sleep 1
    done
    fail "GET $1: no answer"
}
expect() {   # <path> <fixed string>
    fetch "$1" | grep -qF "$2" || fail "GET $1 lacks: $2"
    echo "   ok: GET $1 has $2"
}

copy_candidate() {
    # Tracked files as they are on disk; one deleted but not yet committed is skipped.
    (cd "$ROOT" && git ls-files -z | while IFS= read -r -d '' f; do [ ! -e "$f" ] || printf '%s\0' "$f"; done \
        | tar --null -T - -cf -) | "$ENGINE" exec -i "$NAME" tar -xf - -C "$DIR"
}

check_luci() {
    local page
    for _ in $(seq 20); do
        page="$(on_router 'wget -qO- http://127.0.0.1/cgi-bin/luci/' 2>/dev/null)" \
            && [[ "$page" == *'<html'* && "$page" == *'/luci-static/resources/'* ]] && return 0
        sleep 1
    done
    fail "LuCI did not render its page"
}

on_router "mkdir -p $DIR"
if [ "$SCENARIO" = migrate ]; then
    echo "== Install LuCI and $BASELINE before migrating into this checkout"
    # Exercise the real uhttpd plugins, not only an empty dashboard-only rootfs.
    on_router 'if command -v apk >/dev/null; then apk update && apk add luci uhttpd-mod-ubus uhttpd-mod-ucode; else opkg update && opkg install luci uhttpd-mod-ubus; fi'
    on_router '/etc/init.d/rpcd start && /etc/init.d/uhttpd restart'
    check_luci
    # CI fetches tags. Use the actual published source layout, not a candidate
    # with only VERSION changed to pretend it is old.
    git -C "$ROOT" archive "$BASELINE" | "$ENGINE" exec -i "$NAME" tar -xf - -C "$DIR"
    on_router "printf '%s\n' '{\"title\":\"kept-by-migration\",\"ping_targets\":[],\"history\":{\"record_interval\":10}}' > $DIR/config.json"
    on_router "cd $DIR && LAN_IP=$IP sh openwrt/install.sh"
    expect /api/version '"version":"0.2.1"'
    expect /api/history/tick '"status":"ok"'
    fetch '/api/history?hours=1' > "$SNAPSHOT/api.json"
    on_router "cat $DIR/logs/history.jsonl" > "$SNAPSHOT/history.jsonl"
    [ -s "$SNAPSHOT/history.jsonl" ] || fail "baseline did not record history"
    on_router "cat $DIR/www/config.json" > "$SNAPSHOT/config.json"
    # A real gzip archive containing an old record must survive source extraction
    # and reinstall byte-for-byte, even though the API serves the active JSONL.
    on_router "head -n 1 $DIR/logs/history.jsonl | gzip > $DIR/logs/history-2026-01-01T000000.jsonl.gz"
    archive_hash="$(on_router "sha256sum $DIR/logs/history-2026-01-01T000000.jsonl.gz")"
    check_luci

    echo "== Manual source migration to candidate"
    copy_candidate
    on_router "cd $DIR && LAN_IP=$IP sh lua/install-openwrt.sh"
    version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
    expect /api/version "\"version\":\"$version\""
    # VERSION can still equal the baseline before release preparation. Check the
    # actual configured handler and the new backend identity as well.
    [ "$(on_router 'uci get uhttpd.dashboard.lua_handler')" = "$DIR/lua/handler.lua" ] || fail "old handler still configured"
    expect /api/list-endpoints '"lua"'
    expect /api/4/load '"min1"'
    expect / '/static/app.js?v='
    on_router "cat $DIR/www/config.json" | cmp - "$SNAPSHOT/config.json"
    on_router "head -c $(wc -c < "$SNAPSHOT/history.jsonl") $DIR/logs/history.jsonl" | cmp - "$SNAPSHOT/history.jsonl"
    [ "$(on_router "sha256sum $DIR/logs/history-2026-01-01T000000.jsonl.gz")" = "$archive_hash" ] || fail "history archive changed"
    on_router "gzip -t $DIR/logs/history-2026-01-01T000000.jsonl.gz"
    check_luci
    on_router '/etc/init.d/uhttpd restart'
    restarted_at="$(on_router 'date +%s')"
    sleep 11
    expect /api/history/tick '"status":"ok"'
    fetch '/api/history?hours=1' > "$SNAPSHOT/after.json"
    python3 - "$SNAPSHOT/api.json" "$SNAPSHOT/after.json" "$restarted_at" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

before, after = (json.loads(Path(path).read_text())["records"] for path in sys.argv[1:3])
assert before, "baseline API returned no records"
by_time = {record["timestamp"]: record for record in after}
for record in before:
    assert by_time.get(record["timestamp"]) == record, "old record lost or changed in API"
assert max(datetime.fromisoformat(ts).timestamp() for ts in by_time) > int(sys.argv[3]), "recording did not resume after restart"
PY
    check_luci
    echo "== OpenWrt migration, retained history and LuCI passed"
    exit 0
fi

echo "== Fresh install of this checkout"
copy_candidate
on_router "cd $DIR && LAN_IP=$IP sh lua/install-openwrt.sh"
version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
expect /api/version "\"version\":\"$version\""
expect /api/4/load '"min1"'
expect / '/static/app.js?v='
# Rates come from the previous poll's counters: the second request takes that path.
expect /api/4/cpu '"total"'
sleep 1
expect /api/4/cpu '"total"'
expect /api/history/tick '"status":"ok"'
expect /api/history '"timestamp"'

echo "== Update to the newest release"
# main's VERSION is the newest release (it changes only in `make release`).
latest="$(curl -fsS "https://raw.githubusercontent.com/$REPO/main/VERSION")" \
    || fail "could not read main's VERSION from GitHub"
latest="$(printf '%s' "$latest" | tr -d '[:space:]')"
[ -n "$latest" ] || fail "main's VERSION on GitHub is empty"
if [ "$latest" = 0.0.0 ]; then
    echo "   skipped: $REPO has no release yet"
    exit 0
fi
echo "   newest release: v$latest"
# Pose as an older box with its own config; the update must keep the config.
on_router "cd $DIR && echo 0.0.0 > VERSION && sed -i 's/\"title\": *\"[^\"]*\"/\"title\": \"kept-by-update\"/' www/config.json"
on_router "cd $DIR && LAN_IP=$IP sh lua/install-openwrt.sh --update"
expect /api/version "\"version\":\"$latest\""
expect /config.json '"title": "kept-by-update"'
echo "== OpenWrt install and update passed"
