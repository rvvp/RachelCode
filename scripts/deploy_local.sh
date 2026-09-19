#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${DEPLOY_TARGET_DIR:-/Users/apple/CatalogBackendDeploy}"
APP_LABEL="${DEPLOY_APP_LABEL:-com.catalogbackend.app}"
RESTART_APP="${DEPLOY_RESTART_APP:-1}"
RUN_HEALTHCHECK="${DEPLOY_HEALTHCHECK:-1}"
EXPECTED_BUILD_VERSION="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"

if [ "$ROOT_DIR" = "$TARGET_DIR" ]; then
  echo "源目录和目标目录不能相同。" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"

RSYNC_ARGS=(
  -a
  --delete
  --exclude
  ".git"
  --exclude
  ".venv"
  --exclude
  "__pycache__"
  --exclude
  "*.pyc"
  --exclude
  ".DS_Store"
  --exclude
  ".env"
  --exclude
  "data/"
  --exclude
  "logs/"
  --exclude
  "backups/"
  --exclude
  "dist/"
  --exclude
  ".run-8765.log"
)

echo "开始同步代码到: $TARGET_DIR"
echo "保护目标目录中的 .env、data/、logs/、backups/ 和 .venv"
rsync "${RSYNC_ARGS[@]}" "$ROOT_DIR/" "$TARGET_DIR/"

if [ ! -f "$TARGET_DIR/.env" ]; then
  echo "WARN 目标目录尚未创建 .env，请先在 $TARGET_DIR 配置正式环境参数。"
fi

if [ "$RESTART_APP" = "1" ]; then
  TARGET_PLIST="$HOME/Library/LaunchAgents/$APP_LABEL.plist"
  if [ -f "$TARGET_PLIST" ]; then
    echo "尝试重启 launchd 服务: $APP_LABEL"
    launchctl kickstart -k "gui/$(id -u)/$APP_LABEL"
  else
    echo "WARN 未找到 $TARGET_PLIST，已跳过自动重启。"
  fi
fi

if [ "$RUN_HEALTHCHECK" = "1" ] && [ -f "$TARGET_DIR/.env" ]; then
  PORT="$(sed -n 's/^CATALOG_PORT=//p' "$TARGET_DIR/.env" | tail -n 1 | tr -d '"' | tr -d "'")"
  HOST="$(sed -n 's/^CATALOG_HOST=//p' "$TARGET_DIR/.env" | tail -n 1 | tr -d '"' | tr -d "'")"
  if [ -z "$PORT" ]; then
    PORT="8765"
  fi
  if [ -z "$HOST" ] || [ "$HOST" = "0.0.0.0" ]; then
    HOST="127.0.0.1"
  fi
  HEALTH_URL="http://$HOST:$PORT/healthz"
  echo "检查健康接口: $HEALTH_URL"
  sleep 2
  if HEALTH_PAYLOAD="$(curl --fail --silent "$HEALTH_URL")"; then
    if [ -n "$EXPECTED_BUILD_VERSION" ] && print -r -- "$HEALTH_PAYLOAD" | grep -Fq "\"build_version\": \"$EXPECTED_BUILD_VERSION\""; then
      echo "OK 服务健康检查通过，构建版本: $EXPECTED_BUILD_VERSION"
    else
      echo "ERROR 健康接口仍未加载当前构建版本: $EXPECTED_BUILD_VERSION" >&2
      print -r -- "$HEALTH_PAYLOAD" >&2
      exit 1
    fi
  else
    echo "ERROR 健康检查未通过，请确认服务已重启并检查服务日志。" >&2
    exit 1
  fi
fi

echo "本地安全部署完成。"
