#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_FILE="$ROOT_DIR/launchd/com.catalogbackend.app.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.catalogbackend.app.plist"

mkdir -p "$TARGET_DIR"
mkdir -p "$ROOT_DIR/logs"

sed "s#__ROOT_DIR__#$ROOT_DIR#g" "$TEMPLATE_FILE" > "$TARGET_FILE"

launchctl unload "$TARGET_FILE" >/dev/null 2>&1 || true
launchctl load "$TARGET_FILE"

echo "launchd 安装完成: $TARGET_FILE"
echo "已尝试立即启动服务。"
