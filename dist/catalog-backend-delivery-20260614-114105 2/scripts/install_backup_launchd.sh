#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

TEMPLATE_FILE="$ROOT_DIR/launchd/com.catalogbackend.backup.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.catalogbackend.backup.plist"
BACKUP_HOUR="${CATALOG_BACKUP_HOUR:-2}"
BACKUP_MINUTE="${CATALOG_BACKUP_MINUTE:-30}"

mkdir -p "$TARGET_DIR"
mkdir -p "$ROOT_DIR/logs"

sed \
  -e "s#__ROOT_DIR__#$ROOT_DIR#g" \
  -e "s#__BACKUP_HOUR__#$BACKUP_HOUR#g" \
  -e "s#__BACKUP_MINUTE__#$BACKUP_MINUTE#g" \
  "$TEMPLATE_FILE" > "$TARGET_FILE"

launchctl unload "$TARGET_FILE" >/dev/null 2>&1 || true
launchctl load "$TARGET_FILE"

echo "定时备份 launchd 安装完成: $TARGET_FILE"
echo "每日执行时间: ${BACKUP_HOUR}:${BACKUP_MINUTE}"
