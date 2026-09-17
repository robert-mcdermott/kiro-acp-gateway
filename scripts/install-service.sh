#!/bin/sh
# Install kiro-gateway as a user service (launchd on macOS, systemd --user on Linux).
# Usage: scripts/install-service.sh [env-file]   (default: .env in the project directory)
# The service runs `uv run kiro-gateway` from this checkout and loads the env file.
set -eu
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-.env}"
cd "$PROJECT_DIR"
if [ ! -f "$ENV_FILE" ]; then
  echo "env file '$ENV_FILE' not found; copy .env.example to .env and set KIRO_GATEWAY_API_KEY first" >&2
  exit 2
fi
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/dev.kiro.acp-gateway.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    uv run kiro-gateway --print-service launchd --env-file "$ENV_FILE" > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed $PLIST (logs: ~/Library/Logs/kiro-gateway.log)"
    echo "manage with: launchctl unload/load $PLIST"
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    uv run kiro-gateway --print-service systemd --env-file "$ENV_FILE" > "$UNIT_DIR/kiro-gateway.service"
    systemctl --user daemon-reload
    systemctl --user enable --now kiro-gateway.service
    echo "installed $UNIT_DIR/kiro-gateway.service"
    echo "manage with: systemctl --user status|restart|stop kiro-gateway; logs: journalctl --user -u kiro-gateway -f"
    echo "to keep it running after logout: loginctl enable-linger $USER"
    ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
