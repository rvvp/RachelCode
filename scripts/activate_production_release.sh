#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="${CATALOG_SERVICE_NAME:-rachel-catalog}"
SERVICE_FILE="${CATALOG_SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"
POST_RELEASE_VERIFY_NAME="${CATALOG_POST_RELEASE_VERIFY_NAME:-rachel-catalog-post-release-verify}"
POST_RELEASE_VERIFY_SERVICE_FILE="${CATALOG_POST_RELEASE_VERIFY_SERVICE_FILE:-/etc/systemd/system/${POST_RELEASE_VERIFY_NAME}.service}"
POST_RELEASE_VERIFY_TIMER_FILE="${CATALOG_POST_RELEASE_VERIFY_TIMER_FILE:-/etc/systemd/system/${POST_RELEASE_VERIFY_NAME}.timer}"
LOCAL_URL="${CATALOG_LOCAL_URL:-http://127.0.0.1:8765}"
PUBLIC_URL="${1:-${CATALOG_PUBLIC_URL:-}}"
VENV_DIR="${CATALOG_VENV_DIR:-/opt/rachelcode/venv}"

# Direct Git deployments can retain an untracked manifest from an older
# release. Refresh it before calculating the expected public version.
GIT_ROOT="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ "$GIT_ROOT" = "$ROOT_DIR" ]; then
  GIT_RELEASE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [[ "$GIT_RELEASE_COMMIT" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    printf '%s\n' "$GIT_RELEASE_COMMIT" | tr '[:upper:]' '[:lower:]' > "$ROOT_DIR/.release-commit"
    chmod 0644 "$ROOT_DIR/.release-commit"
  fi
fi

EXPECTED_BUILD="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"
EXPECTED_SOURCE="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_SOURCE_FINGERPRINT"])')"
EXPECTED_COMMIT="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_COMMIT"])')"

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
if [ "$EXPECTED_COMMIT" = "unknown" ]; then
  echo "ERROR 无法确定待发布提交号，拒绝激活无法追溯的代码。" >&2
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
echo "准备激活提交: $EXPECTED_COMMIT"
echo "准备激活源码指纹: $EXPECTED_SOURCE"

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/requirements.txt"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-catalog.service" "$SERVICE_FILE"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-catalog-post-release-verify.service" "$POST_RELEASE_VERIFY_SERVICE_FILE"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-catalog-post-release-verify.timer" "$POST_RELEASE_VERIFY_TIMER_FILE"
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

# Re-arm a single delayed verification for this release. OnActiveSec has no
# recurring interval, so the timer becomes idle after its one execution.
run_root systemctl restart "${POST_RELEASE_VERIFY_NAME}.timer"

echo "OK 正式服务已重启，本机与公网均已加载本次源码，30 分钟后将再执行一次固定验收。"
