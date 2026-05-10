#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# rdp-heyex.sh — RDP into HEYEX 2 EC2, sharing local images folder
#
# Your local directory /home/ray/projects/heyex-test-images will appear
# inside the Windows RDP session as:
#   \\tsclient\heyex-images\
#
# A shortcut "HEYEX Images (Ubuntu)" already exists on the EC2 Desktop
# and in Documents — double-click it to open the folder inside Windows.
#
# Usage (on your local Ubuntu PC):
#   sudo apt install -y freerdp2-x11   # one-time install
#   chmod +x rdp-heyex.sh
#   ./rdp-heyex.sh
# ──────────────────────────────────────────────────────────────────────────────

HOST="54.154.242.69"
INSTANCE_ID="i-02a7dd1797d85a099"
REGION="eu-west-1"
AWS_PROFILE_NAME="milani"
USER="Administrator"
KEY="$HOME/.ssh/AppWay.pem"
LOCAL_DIR="/home/ray/projects/heyex-test-images"
SHARE_NAME="heyex-images"

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

echo "Sharing: $LOCAL_DIR"
echo "  → inside Windows: \\\\tsclient\\$SHARE_NAME\\"
echo "  → or use the 'HEYEX Images (Ubuntu)' shortcut on the EC2 Desktop"
echo ""
echo "Connecting to $HOST as $USER ..."
echo ""

# ── launch RDP ───────────────────────────────────────────────────────────────
EXTRA_ARGS=()
if [ -n "$PASSWORD" ]; then
  EXTRA_ARGS+=(/p:"$PASSWORD")
fi

xfreerdp \
  /v:"$HOST" \
  /u:"$USER" \
  "${EXTRA_ARGS[@]}" \
  /size:1600x900 \
  /dynamic-resolution \
  /drive:"$SHARE_NAME","$LOCAL_DIR" \
  /clipboard \
  /cert:ignore
