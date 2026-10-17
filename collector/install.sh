#!/bin/bash
# Installs the two root collectors Horatio's hardware cards read:
#   health-collect.sh   every 15 min  -> /var/lib/horatio/state.json
#   health-sensors.sh   every 60 s    -> /var/lib/horatio/sensors.json
# plus their systemd units and, once, the per-machine config. Idempotent: a
# re-run replaces the scripts and units (root-owned copies in /usr/local/bin
# and /etc/systemd/system; the checkout is never executed by root) and keeps
# an existing /etc/horatio/config.json.
#
# Usage: sudo bash install.sh              (../install.sh runs this itself once the
#                                           collector is installed, or with --collector)
#        sudo bash install.sh --uninstall  (units, scripts and snapshots; the config stays)
set -euo pipefail
[ "$(id --user)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
S="$(cd "$(dirname "$0")" && pwd)"
UNITS="horatio-collect.service horatio-collect.timer horatio-sensors.service horatio-sensors.timer"

if [ "${1:-}" = "--uninstall" ]; then
    echo "==> removing the hardware health collectors"
    for unit in $UNITS; do
        [ -e "/etc/systemd/system/$unit" ] || continue
        systemctl disable --now "$unit"
        rm "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    for f in /usr/local/bin/health-collect.sh /usr/local/bin/health-sensors.sh; do
        [ -e "$f" ] && rm "$f"
    done
    [ -d /var/lib/horatio ] && rm --recursive /var/lib/horatio
    echo "    kept /etc/horatio/config.json (this box's tuning; remove it by hand if unwanted)"
    exit 0
fi

echo "==> hardware health collectors (15 min: SEL/ECC, SMART, md, backup; 60 s: fans, temps, power)"
if ! command -v smartctl > /dev/null; then
    cat >&2 <<'WARN'

    !!!  smartmontools is NOT installed: the collector cannot read any drive's SMART data.
    !!!  Every run raises the alarm "smartmontools not installed: drives are not monitored"
    !!!  (red Hardware card) until you `apt install smartmontools` and re-run this installer.

WARN
fi

install -D --mode 0755 --owner root --group root "$S/health-collect.sh" /usr/local/bin/health-collect.sh
install -D --mode 0755 --owner root --group root "$S/health-sensors.sh" /usr/local/bin/health-sensors.sh
# Per-machine policy (SEL fault phrases, SAS defect baselines, fan ceiling,
# backup timer). Installed only if absent so a box's own tuning survives
# re-runs. Source: this checkout's health-config.json (gitignored, this box's)
# when it exists, else the shipped example.
if [ -e /etc/horatio/config.json ]; then
    echo "    /etc/horatio/config.json exists; keeping it"
else
    src="$S/health-config.json"
    [ -e "$src" ] || src="$S/health-config.example.json"
    install -D --mode 0644 --owner root --group root "$src" /etc/horatio/config.json
    echo "    installed /etc/horatio/config.json from $(basename "$src"); edit it for this box (README.md)"
fi
for unit in $UNITS; do
    install --mode 0644 --owner root --group root "$S/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
# Synchronous for a oneshot; a python crash fails the job and stops the install
# here with the traceback. Sensors first: the full run alarms on a missing
# sensors.json. The timers come AFTER: `enable --now` on an installed box fires
# the collect run at once (OnBootSec is long past), which would race this order.
systemctl start horatio-sensors.service \
    || { journalctl --unit horatio-sensors.service --lines 30 --no-pager; exit 1; }
systemctl start horatio-collect.service \
    || { journalctl --unit horatio-collect.service --lines 30 --no-pager; exit 1; }
echo "    first snapshots written"
systemctl enable --now horatio-sensors.timer
systemctl enable --now horatio-collect.timer
