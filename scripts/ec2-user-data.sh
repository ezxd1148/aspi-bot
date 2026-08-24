#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# aspi-bot — EC2 User Data (paste into "User data" when launching the instance)
# ──────────────────────────────────────────────────────────────────────────────
# Pasting this in the EC2 launch wizard → Advanced details → User data
# runs it automatically on first boot.  The instance is ready once you
# SSH in and fill in .env then start the service.
#
# If you want truly unattended boot, embed your .env values below
# (search for "SECRETS" at the bottom of the file).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

exec > /tmp/aspi-boot.log 2>&1

REPO_URL="${REPO_URL:-https://github.com/ezxd1148/aspi-bot.git}"
BRANCH="${BRANCH:-main}"

echo "=== aspi-bot user-data start: $(date) ==="

# ── 1. System deps ──
apt-get update -qq
apt-get install -y -qq git python3 python3-pip python3-venv

# ── 2. Clone repo ──
cd /home/ubuntu
git clone --branch "$BRANCH" "$REPO_URL" aspi-bot
chown -R ubuntu:ubuntu aspi-bot

cd /home/ubuntu/aspi-bot

# ── 3. Virtual env ──
python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip uv
uv pip install --quiet -r src/requirements.txt

# ── 4. Data dir ──
mkdir -p data
chown ubuntu:ubuntu data

# ── 5. Systemd service ──
cp aspi-bot-ec2.service /etc/systemd/system/aspi-bot.service
systemctl daemon-reload
systemctl enable aspi-bot

# ══════════════════════════════════════════════════════════════════════════════
# ── SECRETS — uncomment & edit for unattended boot, or SSH in and do it      ──
# ══════════════════════════════════════════════════════════════════════════════
#cat > .env <<'ENVEOF'
#TELEGRAM_BOT_TOKEN=...
#TELEGRAM_CHANNEL_ID=@your_channel
#ADMIN_CHAT_ID=123456789
#TALLY_API_KEY=tly_...
#FORM_ID=...
#MODE=polling
#ENVEOF

echo "=== aspi-bot user-data complete: $(date) ==="
echo ""
echo "Next: ssh in, create .env, then: sudo systemctl start aspi-bot"
echo ""