#!/usr/bin/env bash
# Pair the bot's WhatsApp number to THIS bridge's session.
# Run on a TTY you can scan QR from. WhatsApp app on the bot phone:
#   Settings → Linked devices → Link a device
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$ROOT/vendor/whatsapp-bridge"
if [[ -f "$ROOT/.env" ]]; then set -a; . "$ROOT/.env"; set +a; fi
: "${WA_ALLOWED_USERS:?set WA_ALLOWED_USERS in .env}"
SESSION="${WA_SESSION_DIR:-data/wa-session}"
SESSION="${SESSION/#\~/$HOME}"
[[ "$SESSION" = /* ]] || SESSION="$ROOT/$SESSION"
mkdir -p "$SESSION"
if [[ ! -d "$BRIDGE/node_modules" ]]; then
  (cd "$BRIDGE" && npm install --no-fund --no-audit --progress=false)
fi
export WHATSAPP_MODE=bot
export WHATSAPP_ALLOWED_USERS="$WA_ALLOWED_USERS"
echo "Session: $SESSION"
echo "Scan the QR with the BOT number's WhatsApp (not your personal one)."
exec node "$BRIDGE/bridge.js" --pair-only --session "$SESSION" --mode bot
