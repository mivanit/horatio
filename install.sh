#!/bin/bash
# Horatio installer and updater. Idempotent: run it after every `git pull`.
#   ./install.sh                 install or update the web service, and the hardware
#                                collector when it is already installed on this box
#   ./install.sh --collector     also install the root hardware collector (first time)
#   ./install.sh --no-optional   do not offer nethogs / sysstat
#   ./install.sh --uninstall     remove the service, the collector and their snapshots
#                                (the checkout, its config and history stay)
set -euo pipefail

# nethogs, getcap/setcap and sendmail live in /usr/sbin, which user shells often lack.
PATH="$PATH:/usr/sbin:/sbin"
# As the user who will run the service: this script calls sudo itself where needed.
# A root run leaves a root-owned .venv, config.json and Chart.js that the service cannot write.
[ "$(id --user)" -ne 0 ] || { echo "Run ./install.sh as your own user, not root or sudo." >&2; exit 1; }
command -v uv > /dev/null || { echo "Error: 'uv' not found. Install from https://docs.astral.sh/uv/" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The unit file's ExecStart= and ReadWritePaths= are word-split by systemd and the
# path goes through sed: whitespace, | and & would silently produce a broken unit.
case "$SCRIPT_DIR" in
    *[[:space:]\|\&]*) echo "Unsupported checkout path (whitespace, | or &): $SCRIPT_DIR" >&2; exit 1 ;;
esac
SERVICE_FILE="horatio.service"

WITH_COLLECTOR=0
OPTIONAL=1
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --collector) WITH_COLLECTOR=1 ;;
        --no-optional) OPTIONAL=0 ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "unknown option: $arg (see the header of this script)" >&2; exit 2 ;;
    esac
done
# The collector counts as installed here when its timer unit exists.
if [ -e /etc/systemd/system/horatio-collect.timer ]; then
    WITH_COLLECTOR=1
fi

if [ "$UNINSTALL" = 1 ]; then
    if [ -e "/etc/systemd/system/$SERVICE_FILE" ]; then
        echo "Uninstalling $SERVICE_FILE..."
        sudo systemctl disable --now "$SERVICE_FILE"
        sudo rm "/etc/systemd/system/$SERVICE_FILE"
    fi
    sudo systemctl daemon-reload
    if [ "$WITH_COLLECTOR" = 1 ]; then
        sudo bash "$SCRIPT_DIR/collector/install.sh" --uninstall
    fi
    echo "Done. The checkout, .venv, logs/ and config.json are untouched."
    exit 0
fi

# --- per-machine config: created from the example once, never overwritten ---
if [ ! -e "$SCRIPT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"
    echo "Created config.json from config.example.json: edit it (title, ping targets, thresholds)."
fi

# --- optional tools, offered only while they are not set up (and only with a terminal) ---
if [ "$OPTIONAL" = 1 ] && [ ! -t 0 ]; then
    echo "No terminal: skipping the optional-tool offers (nethogs, sysstat); run interactively to get them."
    OPTIONAL=0
fi
# The offers install via apt; elsewhere they print the package to install by hand.
apt_or_tell() {   # <package>
    if command -v apt-get > /dev/null; then
        sudo apt-get install -y "$1"
    else
        echo "No apt-get here: install '$1' with your package manager, then re-run ./install.sh" >&2
        return 1
    fi
}
# nethogs needs all four capabilities (the unit's CapabilityBoundingSet= lists the same four).
nethogs_ready() {
    command -v nethogs > /dev/null && command -v getcap > /dev/null \
        && [[ "$(getcap "$(command -v nethogs)")" == *cap_net_admin* ]]
}
if [ "$OPTIONAL" = 1 ]; then
    if ! nethogs_ready; then
        echo "=== Optional: the Network Processes card needs nethogs with capabilities ==="
        read -p "Install and configure nethogs? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            command -v nethogs > /dev/null || apt_or_tell nethogs
            command -v setcap > /dev/null || apt_or_tell libcap2-bin
            sudo setcap cap_net_admin,cap_net_raw,cap_dac_read_search,cap_sys_ptrace+ep "$(command -v nethogs)"
        else
            echo "Skipped: the Network Processes card will show an error (offered again next run; --no-optional silences it)."
        fi
    fi
    if ! command -v pidstat > /dev/null; then
        echo "=== Optional: the Disk I/O Processes card needs sysstat (pidstat) ==="
        read -p "Install sysstat? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            apt_or_tell sysstat
        else
            echo "Skipped: the Disk I/O Processes card will show an error (offered again next run; --no-optional silences it)."
        fi
    fi
fi

# --- System Mail card prerequisites (check only; MTA setup is outside this repo) ---
# Tested by absolute path rather than `command -v`: a non-root PATH often omits /usr/sbin.
if [[ ! -x /usr/sbin/sendmail || ! -x /usr/bin/mail ]]; then
    echo "=== System Mail card: no local MTA ==="
    [[ -x /usr/sbin/sendmail ]] || echo "  /usr/sbin/sendmail missing: nothing will ever be delivered to /var/mail."
    [[ -x /usr/bin/mail ]] || echo "  /usr/bin/mail missing (bsd-mailx): smartd's mail hook exits before sending."
    echo "  The card will show 'No mail spool yet'. It needs a local-only MTA (postfix) delivering"
    echo "  root's mail to this user's spool via /etc/aliases (README: System Mail card)."
    echo ""
fi

echo "Syncing Python environment..."
# --locked: never rewrite uv.lock on the box; fail loudly if it disagrees with pyproject.
uv sync --project "$SCRIPT_DIR" --locked --link-mode copy
[ -x "$SCRIPT_DIR/.venv/bin/python" ] || { echo "Error: .venv/bin/python missing after uv sync" >&2; exit 1; }

echo "Downloading Chart.js..."
# Pinned version + checksum: this file runs in the dashboard's own origin, so a
# tampered or silently-upgraded download must never be served. Bump BOTH together
# on a deliberate upgrade (get the new hash with `sha256sum chart.umd.min.js`).
CHARTJS_VERSION="4.5.1"
CHARTJS_SHA256="48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a"
CHARTJS="$SCRIPT_DIR/static/chart.umd.min.js"
# Verify before replacing: the running service serves this file, so a captive
# portal page or a cut-off download must never land on it.
if echo "${CHARTJS_SHA256}  $CHARTJS" | sha256sum -c --status - >/dev/null 2>&1; then
  echo "Chart.js ${CHARTJS_VERSION} already present."
else
  CHARTJS_TMP="$(mktemp "$SCRIPT_DIR/static/.chart.XXXXXX")"
  curl -fsSL -o "$CHARTJS_TMP" \
    "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js" \
    || { rm -f "$CHARTJS_TMP"; exit 1; }
  echo "${CHARTJS_SHA256}  $CHARTJS_TMP" | sha256sum -c --status - || {
    rm -f "$CHARTJS_TMP"
    echo "Error: Chart.js checksum mismatch: refusing to install an unexpected file." >&2
    exit 1
  }
  chmod 644 "$CHARTJS_TMP"
  mv "$CHARTJS_TMP" "$CHARTJS"
fi

echo "Installing $SERVICE_FILE..."
# The template's placeholders /opt/horatio and User=horatio become this checkout
# and the invoking user (never root: refused at the top).
SERVICE_USER="$(id --user --name)"
sed -e "s|/opt/horatio|$SCRIPT_DIR|g" \
    -e "s|^User=horatio|User=$SERVICE_USER|" \
    "$SCRIPT_DIR/$SERVICE_FILE" | sudo tee "/etc/systemd/system/$SERVICE_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_FILE"
# restart, not `enable --now`: a reinstall over a running service must pick up the
# re-synced venv and the new unit.
sudo systemctl restart "$SERVICE_FILE"

if [ "$WITH_COLLECTOR" = 1 ]; then
    sudo bash "$SCRIPT_DIR/collector/install.sh"
fi

# `systemctl status` exits non-zero for a dead service and is noisy on every update;
# check liveness, and only on failure show the journal.
sleep 3
if sudo systemctl is-active --quiet "$SERVICE_FILE"; then
    echo "Done: $SERVICE_FILE is running as $SERVICE_USER (http://localhost:8080)."
else
    echo "Error: $SERVICE_FILE is not running:" >&2
    sudo journalctl --unit "$SERVICE_FILE" --lines 30 --no-pager
    exit 1
fi
