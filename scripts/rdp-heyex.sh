#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# rdp-heyex.sh — RDP into HEYEX 2 EC2 with auto-password + local folder share
#                + 4K-friendly display (smart-sizing stretch)
#
# Your local /home/ray/projects/heyex-test-images appears inside Windows as:
#   \\tsclient\heyex-images\
# A Desktop shortcut "HEYEX Images (Ubuntu)" opens that folder.
#
# Usage (on your local Ubuntu PC):
#   sudo apt install -y freerdp2-x11   # one-time install
#   chmod +x rdp-heyex.sh
#   ./rdp-heyex.sh
#
# Tuning for different monitors (override via env vars):
#   HEYEX_SIZE=1920x1080  ./rdp-heyex.sh   # 4K monitor — 2× stretch (default)
#   HEYEX_SIZE=1600x900   ./rdp-heyex.sh   # 4K monitor — even bigger (2.4×)
#   HEYEX_SIZE=1280x720   ./rdp-heyex.sh   # 4K monitor — max size (3×, fuzzy)
#   HEYEX_SIZE=1920x1080 HEYEX_SMARTSIZE=1920x1080 ./rdp-heyex.sh  # 1080p native
# ──────────────────────────────────────────────────────────────────────────────

HOST="54.154.242.69"
INSTANCE_ID="i-02a7dd1797d85a099"
REGION="eu-west-1"
AWS_PROFILE_NAME="milani"
USER="Administrator"
KEY="$HOME/.ssh/AppWay.pem"
LOCAL_DIR="/home/ray/projects/heyex-test-images"
SHARE_NAME="heyex-images"

# Display tuning:
#   HEYEX_SIZE      = RDP session canvas (smaller = bigger-looking icons)
#   HEYEX_SMARTSIZE = physical monitor resolution (smart-sizing stretches to this)
# Defaults are tuned for a 4K (3840x2160) monitor:
HEYEX_SIZE="${HEYEX_SIZE:-1600x900}"
HEYEX_SMARTSIZE="${HEYEX_SMARTSIZE:-3840x2160}"

# ── ensure xfreerdp is installed ─────────────────────────────────────────────
if ! command -v xfreerdp &>/dev/null; then
  echo "xfreerdp not found — installing (requires sudo)..."
  sudo apt install -y freerdp2-x11
fi

# ── ensure local share dir exists ────────────────────────────────────────────
mkdir -p "$LOCAL_DIR"

# ── auto-fetch Administrator password via AWS CLI ────────────────────────────
PASSWORD=""
if command -v aws &>/dev/null && [ -f "$KEY" ]; then
  echo "Fetching Administrator password from AWS (profile: $AWS_PROFILE_NAME)..."
  PASSWORD=$(AWS_PROFILE="$AWS_PROFILE_NAME" aws ec2 get-password-data \
    --instance-id "$INSTANCE_ID" \
    --priv-launch-key "$KEY" \
    --region "$REGION" \
    --query PasswordData --output text 2>/dev/null | tr -d '\n')
fi

if [ -n "$PASSWORD" ]; then
  echo "✅ Password retrieved — connecting automatically."
  echo ""
else
  echo "⚠️  Could not auto-fetch password (AWS CLI not configured or key not found)."
  echo "   You will be prompted to enter it manually."
  echo "   Tip: aws configure --profile milani  (then re-run this script)"
  echo ""
fi

echo "Sharing : $LOCAL_DIR"
echo "  → inside Windows: \\\\tsclient\\$SHARE_NAME\\"
echo "  → or use the 'HEYEX Images (Ubuntu)' shortcut on the EC2 Desktop"
echo ""
echo "Display : session ${HEYEX_SIZE} stretched to ${HEYEX_SMARTSIZE}"
echo "Connecting to $HOST as $USER ..."
echo ""

# ── launch RDP ───────────────────────────────────────────────────────────────
EXTRA_ARGS=()
[ -n "$PASSWORD" ] && EXTRA_ARGS+=(/p:"$PASSWORD")

xfreerdp \
  /v:"$HOST" \
  /u:"$USER" \
  "${EXTRA_ARGS[@]}" \
  /size:"$HEYEX_SIZE" \
  /smart-sizing:"$HEYEX_SMARTSIZE" \
  /drive:"$SHARE_NAME","$LOCAL_DIR" \
  /clipboard \
  /cert:ignore
