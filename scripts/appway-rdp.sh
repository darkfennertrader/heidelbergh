#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# appway-rdp.sh — RDP into the AppWay Link Windows EC2
#                 with auto-password + 4K-friendly display (smart-sizing stretch)
#
# Usage (on your local Ubuntu PC):
#   sudo apt install -y freerdp2-x11   # one-time install
#   chmod +x scripts/appway-rdp.sh
#   ./scripts/appway-rdp.sh
#
# Tuning for different monitors (override via env vars):
#   APPWAY_SIZE=1920x1080  ./scripts/appway-rdp.sh   # 4K monitor — 2× stretch (default)
#   APPWAY_SIZE=1600x900   ./scripts/appway-rdp.sh   # 4K monitor — even bigger (2.4×)
#   APPWAY_SIZE=1280x720   ./scripts/appway-rdp.sh   # 4K monitor — max size (3×, fuzzy)
#   APPWAY_SIZE=1920x1080 APPWAY_SMARTSIZE=1920x1080 ./scripts/appway-rdp.sh  # 1080p native
#
# --test mode:
#   ./scripts/appway-rdp.sh --test   # verify password retrieval + RDP port, then exit
# ──────────────────────────────────────────────────────────────────────────────

HOST="52.18.26.234"
INSTANCE_ID="i-02a99abeba370f0a7"
REGION="eu-west-1"
AWS_PROFILE_NAME="Milani"
USER="Administrator"
KEY="$HOME/.ssh/AppWay.pem"

# Display tuning:
#   APPWAY_SIZE      = RDP session canvas (smaller = bigger-looking icons)
#   APPWAY_SMARTSIZE = physical monitor resolution (smart-sizing stretches to this)
# Defaults are tuned for a 4K (3840x2160) monitor:
APPWAY_SIZE="${APPWAY_SIZE:-1600x900}"
APPWAY_SMARTSIZE="${APPWAY_SMARTSIZE:-3840x2160}"

MODE="${1:-}"

# ── help ─────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/appway-rdp.sh [--test]

Default behavior:
  Fetch the Windows Administrator password from AWS and open an RDP session
  into the AppWay Link EC2 (52.18.26.234 / i-02a99abeba370f0a7).

Options:
  --test    Verify AWS password retrieval and RDP port reachability, then exit.
  -h, --help

Optional environment variables:
  APPWAY_SIZE        RDP session canvas, e.g. 1920x1080 (default: 1600x900)
  APPWAY_SMARTSIZE   Physical monitor resolution for smart-sizing (default: 3840x2160)
EOF
  exit 0
fi

# ── ensure xfreerdp is installed ─────────────────────────────────────────────
if ! command -v xfreerdp &>/dev/null; then
  echo "xfreerdp not found — installing (requires sudo)..."
  sudo apt install -y freerdp2-x11
fi

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
  echo "   Tip: aws configure --profile Milani  (then re-run this script)"
  echo ""
fi

# ── --test mode: verify and exit without launching ───────────────────────────
if [[ "$MODE" == "--test" ]]; then
  if command -v nc &>/dev/null; then
    nc -vz -w 5 "$HOST" 3389 >/dev/null && echo "✅ RDP port 3389 reachable on $HOST"
  else
    echo "nc not installed — skipping RDP port check"
  fi
  echo "AWS password retrieval: $( [ -n "$PASSWORD" ] && echo '✅ success' || echo '⚠️  failed' )"
  echo "Display: session ${APPWAY_SIZE} stretched to ${APPWAY_SMARTSIZE}"
  exit 0
fi

echo "Display : session ${APPWAY_SIZE} stretched to ${APPWAY_SMARTSIZE}"
echo "Connecting to $HOST as $USER ..."
echo ""

# ── launch RDP ───────────────────────────────────────────────────────────────
EXTRA_ARGS=()
[ -n "$PASSWORD" ] && EXTRA_ARGS+=(/p:"$PASSWORD")

xfreerdp \
  /v:"$HOST" \
  /u:"$USER" \
  "${EXTRA_ARGS[@]}" \
  /size:"$APPWAY_SIZE" \
  /smart-sizing:"$APPWAY_SMARTSIZE" \
  /clipboard \
  /cert:ignore
