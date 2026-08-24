#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# aspi-bot — Deploy to an EC2 instance in one shot
# ──────────────────────────────────────────────────────────────────────────────
# Run from your local machine:
#
#   export ASPI_HOST=ubuntu@<ec2-public-ip>
#   ./scripts/deploy-fresh.sh
#
# This pushes the code + .env then runs setup-ec2.sh remotely.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOST="${ASPI_HOST:-}"
REMOTE_DIR="${ASPI_DIR:-/home/ubuntu/aspi-bot}"

if [ -z "$HOST" ]; then
    echo "Usage: ASPI_HOST=ubuntu@<ec2-ip> $0"
    echo ""
    echo "Set ASPI_HOST to the SSH destination."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "ERROR: No .env file found in the project root."
    echo "Create one from .env.example before deploying."
    exit 1
fi

echo "=== Deploying aspi-bot to $HOST ==="

# ── Rsync the project (exclude venv, node_modules, git, data) ──
echo "--- Syncing code ---"
rsync -az --delete \
    --exclude '.venv' \
    --exclude '.git' \
    --exclude 'data' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'node_modules' \
    ./ "$HOST:$REMOTE_DIR"

# ── Run remote setup ──
echo "--- Running remote setup ---"
ssh -t "$HOST" "bash $REMOTE_DIR/scripts/setup-ec2.sh"

echo ""
echo "=== Deploy finished ==="
echo "    SSH in and start the service:"
echo "    ssh $HOST"
echo "    sudo systemctl start aspi-bot"
echo "    sudo journalctl -u aspi-bot -f"
echo ""