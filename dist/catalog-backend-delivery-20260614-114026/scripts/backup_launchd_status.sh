#!/bin/zsh
set -euo pipefail

TARGET_LABEL="com.catalogbackend.backup"
TARGET_FILE="$HOME/Library/LaunchAgents/com.catalogbackend.backup.plist"

echo "LaunchAgent 文件: $TARGET_FILE"
if [ -f "$TARGET_FILE" ]; then
  echo "OK 已安装"
else
  echo "WARN 未安装"
fi

echo
echo "launchctl 状态:"
launchctl list | grep "$TARGET_LABEL" || echo "当前未加载 $TARGET_LABEL"
