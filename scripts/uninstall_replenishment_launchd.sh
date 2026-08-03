#!/bin/zsh
set -euo pipefail

TARGET_FILE="$HOME/Library/LaunchAgents/com.replenishmentcenter.app.plist"
DOMAIN="gui/$(id -u)"

if [ -f "$TARGET_FILE" ]; then
  launchctl bootout "$DOMAIN" "$TARGET_FILE" >/dev/null 2>&1 || true
  rm "$TARGET_FILE"
fi

echo "补货中心本机服务已卸载。"
