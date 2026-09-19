#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="${CATALOG_SERVICE_NAME:-rachel-catalog}"
SERVICE_FILE="${CATALOG_SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"
LOCAL_URL="${CATALOG_LOCAL_URL:-http://127.0.0.1:8765}"
PUBLIC_URL="${1:-${CATALOG_PUBLIC_URL:-}}"
VENV_DIR="${CATALOG_VENV_DIR:-/opt/rachelcode/venv}"
EXPECTED_BUILD="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"
EXPECTED_SOURCE="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_SOURCE_FINGERPRINT"])')"

if [ "$(uname -s)" != "Linux" ]; then
  echo "ERROR 正式服务器激活脚本只能在 Linux 服务器上运行。" >&2
  exit 1
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "ERROR 未找到正式环境 Python: $VENV_DIR/bin/python" >&2
  exit 1
fi
if [ ! -x "$VENV_DIR/bin/gunicorn" ]; then
  echo "ERROR 未找到 Gunicorn: $VENV_DIR/bin/gunicorn" >&2
  exit 1
fi

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

echo "准备激活构建: $EXPECTED_BUILD"
echo "准备激活源码指纹: $EXPECTED_SOURCE"

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/requirements.txt"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-catalog.service" "$SERVICE_FILE"
run_root systemctl daemon-reload
run_root systemctl restart "$SERVICE_NAME"

for attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 5 "$LOCAL_URL/healthz" >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "ERROR 服务重启后 30 秒内未通过本机健康检查。" >&2
    run_root systemctl status "$SERVICE_NAME" --no-pager || true
    run_root journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
    exit 1
  fi
  sleep 1
done

run_root systemctl is-active --quiet "$SERVICE_NAME"
CATALOG_DEPLOYMENT_CHECK_COUNT=256 CATALOG_EXPECTED_WORKERS=8 "$ROOT_DIR/scripts/verify_public_deployment.sh" "$LOCAL_URL"

if [ -n "$PUBLIC_URL" ]; then
  CATALOG_DEPLOYMENT_CHECK_COUNT=256 CATALOG_EXPECTED_WORKERS=8 "$ROOT_DIR/scripts/verify_public_deployment.sh" "$PUBLIC_URL"
else
  echo "ERROR 未传入公网地址，无法完成发布后公网验收。" >&2
  echo "用法: $0 http://203.205.90.232:8765" >&2
  exit 1
fi

echo "OK 正式服务已重启，本机与公网均已加载本次源码。"
