#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# aspi-bot — management helpers
# ──────────────────────────────────────────────────────────────────────────────
# Source this file (or symlink into PATH) for quick commands:
#
#   source scripts/manage.sh
#   aspi-logs
#   aspi-restart
# ──────────────────────────────────────────────────────────────────────────────

aspi-status() {
    sudo systemctl status aspi-bot "$@"
}

aspi-logs() {
    sudo journalctl -u aspi-bot -n 50 -f "$@"
}

aspi-start() {
    sudo systemctl start aspi-bot
    echo "Started.  Logs: sudo journalctl -u aspi-bot -f"
}

aspi-stop() {
    sudo systemctl stop aspi-bot
}

aspi-restart() {
    sudo systemctl restart aspi-bot
    echo "Restarted.  Logs: sudo journalctl -u aspi-bot -f"
}

aspi-enable() {
    sudo systemctl enable aspi-bot
}

aspi-update() {
    # Pull latest code, rebuild deps, restart
    OLD_DIR=$(pwd)
    cd "${INSTALL_DIR:-/home/ubuntu/aspi-bot}" || {
        echo "Set INSTALL_DIR or cd to the repo first."
        return 1
    }

    echo "--- Pulling latest ---"
    git pull

    echo "--- Updating deps ---"
    source .venv/bin/activate
    uv pip install --quiet -r src/requirements.txt

    echo "--- Restarting ---"
    sudo systemctl restart aspi-bot
    echo "Updated & restarted."

    cd "$OLD_DIR"
}

echo "aspi-bot helpers loaded:"
echo "  aspi-status   — status of the daemon"
echo "  aspi-logs     — tail the log"
echo "  aspi-start    — start the daemon"
echo "  aspi-stop     — stop the daemon"
echo "  aspi-restart  — restart the daemon"
echo "  aspi-update   — git pull + pip install + restart"
echo ""