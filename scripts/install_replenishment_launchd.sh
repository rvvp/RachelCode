#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_FILE="$ROOT_DIR/launchd/com.replenishmentcenter.app.plist.template"
DEPLOY_DIR="${REPLENISH_DEPLOY_DIR:-$HOME/ReplenishmentCenterDeploy}"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/com.replenishmentcenter.app.plist"
DOMAIN="gui/$(id -u)"
LABEL="com.replenishmentcenter.app"

mkdir -p "$TARGET_DIR" "$DEPLOY_DIR" "$DEPLOY_DIR/logs" "$DEPLOY_DIR/scripts"

rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT_DIR/replenishment_center/" "$DEPLOY_DIR/replenishment_center/"
rsync -a "$ROOT_DIR/replenishment_app.py" "$ROOT_DIR/requirements.txt" "$DEPLOY_DIR/"
rsync -a "$ROOT_DIR/scripts/vipshop_browser_worker.js" "$DEPLOY_DIR/scripts/"

if [ ! -x "$DEPLOY_DIR/.venv/bin/python3" ]; then
  rsync -a "$ROOT_DIR/.venv/" "$DEPLOY_DIR/.venv/"
fi
if [ ! -f "$DEPLOY_DIR/replenishment_data/replenishment.db" ]; then
  mkdir -p "$DEPLOY_DIR/replenishment_data"
  rsync -a "$ROOT_DIR/replenishment_data/" "$DEPLOY_DIR/replenishment_data/"
fi

"$DEPLOY_DIR/.venv/bin/python3" -c 'import openpyxl'
sed "s#__DEPLOY_DIR__#$DEPLOY_DIR#g" "$TEMPLATE_FILE" > "$TARGET_FILE"
plutil -lint "$TARGET_FILE" >/dev/null

launchctl bootout "$DOMAIN" "$TARGET_FILE" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$TARGET_FILE"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "补货中心本机服务已安装并启动: $TARGET_FILE"
echo "独立运行目录: $DEPLOY_DIR"
