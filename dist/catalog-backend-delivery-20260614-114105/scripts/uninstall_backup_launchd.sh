#!/bin/zsh
set -euo pipefail

TARGET_FILE="$HOME/Library/LaunchAgents/com.catalogbackend.backup.plist"

if [ -f "$TARGET_FILE" ]; then
  launchctl unload "$TARGET_FILE" >/dev/null 2>&1 || true
  rm -f "$TARGET_FILE"
  echo "已移除定时备份 launchd 配置: $TARGET_FILE"
else
  echo "未找到定时备份 launchd 配置: $TARGET_FILE"
fi
