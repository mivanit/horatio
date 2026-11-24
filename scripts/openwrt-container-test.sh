#!/usr/bin/env bash
# Install, then update, the dashboard on real OpenWrt: the official rootfs image
# booted with its own init (procd), uhttpd and package feeds. CI runs it on every
# push (.github/workflows/check.yml); locally:
#   scripts/openwrt-container-test.sh          # docker; CONTAINER=podman to switch
# OPENWRT_IMAGE picks another image. Needs network: the package feeds, Chart.js,
# and GitHub for the update.
#
# 1. The checkout's tracked files (no .git, as a copied-over router has them) are
#    installed fresh, and the dashboard must answer with this checkout's VERSION.
# 2. Once there is a release (main's VERSION on GitHub is past 0.0.0): the box's
#    VERSION is set to 0.0.0 and `--update` must download and install that
#    release, keeping www/config.json.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${CONTAINER:-docker}"
IMAGE="${OPENWRT_IMAGE:-docker.io/openwrt/rootfs:x86-64-openwrt-25.12}"
NAME="horatio-openwrt-test-$$"
REPO="mivanit/horatio"
DIR=/root/horatio

fail() { echo "FAIL: $*" >&2; exit 1; }
on_router() { "$ENGINE" exec "$NAME" sh -c "$1"; }
cleanup() {
    if [ "${1:-0}" != 0 ]; then
        echo "--- router log (uhttpd) ---" >&2
        on_router 'logread | grep -i uhttpd | tail -20' >&2 || true
    fi
    "$ENGINE" rm -f "$NAME" > /dev/null 2>&1 || true
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

echo "== Fresh install of this checkout"
on_router "mkdir -p $DIR"
# Tracked files as they are on disk; one deleted but not yet committed is skipped.
(cd "$ROOT" && git ls-files -z | while IFS= read -r -d '' f; do [ ! -e "$f" ] || printf '%s\0' "$f"; done \
    | tar --null -T - -cf -) | "$ENGINE" exec -i "$NAME" tar -xf - -C "$DIR"
on_router "cd $DIR && LAN_IP=$IP sh lua/install.sh"
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
on_router "cd $DIR && LAN_IP=$IP sh lua/install.sh --update"
expect /api/version "\"version\":\"$latest\""
expect /config.json '"title": "kept-by-update"'
echo "== OpenWrt install and update passed"
