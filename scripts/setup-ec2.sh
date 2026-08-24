#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# aspi-bot — EC2 setup script
# ──────────────────────────────────────────────────────────────────────────────
# Run this ONCE on a fresh Ubuntu EC2 instance to bootstrap everything.
#
#   curl -fsSL https://raw.githubusercontent.com/.../setup-ec2.sh | bash
#
# Or scp it up and run locally:
#
#   scp scripts/setup-ec2.sh ubuntu@<ip>:
#   ssh ubuntu@<ip> ./setup-ec2.sh
# ──────────────────────────────────────────────────────────────────────────────

REPO_URL="${REPO_URL:-https://github.com/ezxd1148/aspi-bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/home/ubuntu/aspi-bot}"

echo "=== aspi-bot EC2 setup ==="
echo "  Install dir: $INSTALL_DIR"

# ── 1. System deps ────────────────────────────────────────────────────────────
echo "--- Installing system packages ---"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-pip python3-venv

# ── 2. Code ──────────────────────────────────────────────────────────────────
# Three scenarios:
#   A) rsync deploy (files exist, no .git)  → skip git, already up to date
#   B) fresh install (nothing exists)        → git clone
#   C) git pull (has .git)                   → git pull

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "--- Git repo found — pulling latest ---"
    cd "$INSTALL_DIR"
    git pull
elif [ -d "$INSTALL_DIR" ]; then
    # rsync deploy — files already there, no git needed
    echo "--- Files found (rsync deploy) — skipping git ---"
    cd "$INSTALL_DIR"
else
    echo "--- Cloning repository ($BRANCH) ---"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 3. Python virtual env ─────────────────────────────────────────────────────
echo "--- Creating virtualenv ---"
python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet uv  # fast installer
uv pip install --quiet -r src/requirements.txt

# ── 4. .env ───────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  No .env file found."
    echo "  Copy .env.example to .env and fill in your credentials:"
    echo ""
    echo "    cp .env.example .env"
    echo "    nano .env"
    echo ""
    echo "  Then start the service:"
    echo "    sudo systemctl start aspi-bot"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo ""
else
    echo "--- .env already exists, keeping it ---"
fi

# ── 5. Data dir ───────────────────────────────────────────────────────────────
mkdir -p data

# ── 6. Install systemd service ────────────────────────────────────────────────
echo "--- Installing systemd service ---"
sudo cp aspi-bot-ec2.service /etc/systemd/system/aspi-bot.service
sudo systemctl daemon-reload
sudo systemctl enable aspi-bot

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Next steps:"
if [ ! -f ".env" ]; then
    echo "    1. cp .env.example .env"
    echo "    2. nano .env          # fill in your API keys & IDs"
fi
echo "    3. sudo systemctl start aspi-bot"
echo "    4. sudo journalctl -u aspi-bot -f   # watch the logs"
echo ""