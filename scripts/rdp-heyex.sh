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
USER="Administrator"
LOCAL_DIR="/home/ray/projects/heyex-test-images"
SHARE_NAME="heyex-images"

# ── ensure xfreerdp is installed ─────────────────────────────────────────────
if ! command -v xfreerdp &>/dev/null; then
  echo "xfreerdp not found — installing (requires sudo)..."
  sudo apt install -y freerdp2-x11
fi

# ── ensure local share dir exists ────────────────────────────────────────────
mkdir -p "$LOCAL_DIR"

echo "Sharing: $LOCAL_DIR"
echo "  → inside Windows: \\\\tsclient\\$SHARE_NAME\\"
echo "  → or use the 'HEYEX Images (Ubuntu)' shortcut on the EC2 Desktop"
echo ""
echo "Connecting to $HOST as $USER ..."
echo "(enter the EC2 Administrator password when prompted)"
echo ""

# ── launch RDP ───────────────────────────────────────────────────────────────
xfreerdp \
  /v:"$HOST" \
  /u:"$USER" \
  /size:1600x900 \
  /dynamic-resolution \
  /drive:"$SHARE_NAME","$LOCAL_DIR" \
  /clipboard \
  /cert:ignore
