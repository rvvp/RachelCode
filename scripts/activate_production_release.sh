#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
SERVICE_NAME="${CATALOG_SERVICE_NAME:-rachel-catalog}"
SERVICE_FILE="${CATALOG_SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"
REPLENISH_SERVICE_NAME="${REPLENISH_SERVICE_NAME:-rachel-replenishment}"
REPLENISH_SERVICE_FILE="${REPLENISH_SERVICE_FILE:-/etc/systemd/system/${REPLENISH_SERVICE_NAME}.service}"
PLANNING_SERVICE_NAME="${PLANNING_SERVICE_NAME:-rachel-planning}"
LEGACY_VERIFY_NAME="rachel-catalog-post-release-verify"
LEGACY_VERIFY_SERVICE_FILE="/etc/systemd/system/${LEGACY_VERIFY_NAME}.service"
LEGACY_VERIFY_TIMER_FILE="/etc/systemd/system/${LEGACY_VERIFY_NAME}.timer"
LOCAL_URL="${CATALOG_LOCAL_URL:-http://127.0.0.1:8765}"
PUBLIC_URL="${1:-${CATALOG_PUBLIC_URL:-}}"
REPLENISH_LOCAL_URL="${REPLENISH_LOCAL_URL:-http://127.0.0.1:8877}"
REPLENISH_PUBLIC_URL="${REPLENISH_PUBLIC_URL:-http://203.205.90.232:8877}"
PLANNING_LOCAL_URL="${PLANNING_LOCAL_URL:-http://127.0.0.1:8785}"
PLANNING_PUBLIC_URL="${PLANNING_PUBLIC_URL:-http://203.205.90.232:8785}"
VENV_DIR="${CATALOG_VENV_DIR:-/opt/rachelcode/venv}"

fail() {
  echo "ERROR $*" >&2
  exit 1
}

if [ "$(uname -s)" != "Linux" ]; then
  fail "正式服务器激活脚本只能在 Linux 服务器上运行。"
fi

# A webhook can reach activation after a failed or stale checkout update.
# Synchronize only a clean main branch, and never reset divergent server state.
GIT_ROOT="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$GIT_ROOT" ]; then
  [ "$GIT_ROOT" = "$ROOT_DIR" ] || fail "激活脚本不在 Git 工作区根目录，拒绝使用不确定的代码路径。"
  GIT_RELEASE_COMMIT="$(PYTHONPATH="$ROOT_DIR" python3 -c '
import sys
from catalog_backend.release import ReleaseCheckoutError, sync_checkout_to_origin_main
try:
    print(sync_checkout_to_origin_main(sys.argv[1], sys.argv[2] or None))
except ReleaseCheckoutError as exc:
    raise SystemExit(f"ERROR {exc}")
' "$ROOT_DIR" "${CATALOG_EXPECTED_RELEASE_COMMIT:-}")" || fail "无法将服务器工作区安全同步到 origin/main。"
  echo "服务器工作区已对齐 origin/main: $GIT_RELEASE_COMMIT"
  GIT_RELEASE_GENERATION="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || true)"
  if [[ "$GIT_RELEASE_COMMIT" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    printf '%s\n' "$GIT_RELEASE_COMMIT" | tr '[:upper:]' '[:lower:]' > "$ROOT_DIR/.release-commit"
    chmod 0644 "$ROOT_DIR/.release-commit"
  fi
  if [[ "$GIT_RELEASE_GENERATION" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' "$GIT_RELEASE_GENERATION" > "$ROOT_DIR/.release-generation"
    chmod 0644 "$ROOT_DIR/.release-generation"
  fi
elif [ -n "${CATALOG_EXPECTED_RELEASE_COMMIT:-}" ]; then
  PACKAGE_COMMIT="$(tr -d '[:space:]' < "$ROOT_DIR/.release-commit" 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)"
  EXPECTED_RELEASE_COMMIT="$(printf '%s' "$CATALOG_EXPECTED_RELEASE_COMMIT" | tr '[:upper:]' '[:lower:]')"
  [ "$PACKAGE_COMMIT" = "$EXPECTED_RELEASE_COMMIT" ] || fail "发布包提交号与自动发布任务要求的不一致。"
fi

EXPECTED_BUILD="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"
EXPECTED_SOURCE="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_SOURCE_FINGERPRINT"])')"
EXPECTED_COMMIT="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_COMMIT"])')"
EXPECTED_GENERATION="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_GENERATION"])')"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  fail "未找到正式环境 Python: $VENV_DIR/bin/python"
fi
if [ ! -x "$VENV_DIR/bin/gunicorn" ]; then
  fail "未找到 Gunicorn: $VENV_DIR/bin/gunicorn"
fi
if [ "$EXPECTED_COMMIT" = "unknown" ]; then
  fail "无法确定待发布提交号，拒绝激活无法追溯的代码。"
fi
if ! [[ "$EXPECTED_GENERATION" =~ ^[1-9][0-9]*$ ]]; then
  fail "无法确定待发布版本序号，拒绝激活可能乱序的代码。"
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
echo "准备激活序号: $EXPECTED_GENERATION"
echo "准备激活源码指纹: $EXPECTED_SOURCE"

# A delayed webhook must never roll production back after a newer release has
# already started. Older builds do not expose a generation and are allowed
# once so this guard can bootstrap itself.
CURRENT_RELEASE="$(curl --fail --silent --max-time 5 "$LOCAL_URL/healthz" 2>/dev/null || true)"
if [ -n "$CURRENT_RELEASE" ]; then
  STALE_REASON="$(cd "$ROOT_DIR" && CURRENT_RELEASE="$CURRENT_RELEASE" EXPECTED_GENERATION="$EXPECTED_GENERATION" EXPECTED_COMMIT="$EXPECTED_COMMIT" python3 -c 'import json,os; from catalog_backend.release import stale_release_reason; p=json.loads(os.environ["CURRENT_RELEASE"]); print(stale_release_reason(p.get("release_generation"), p.get("release_commit"), os.environ["EXPECTED_GENERATION"], os.environ["EXPECTED_COMMIT"]) or "")' 2>/dev/null || true)"
  if [ -n "$STALE_REASON" ]; then
    echo "ERROR 拒绝乱序发布: $STALE_REASON" >&2
    exit 1
  fi
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/requirements.txt"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-catalog.service" "$SERVICE_FILE"
run_root install -m 0644 "$ROOT_DIR/deploy/systemd/rachel-replenishment.service" "$REPLENISH_SERVICE_FILE"
# Retire the removed delayed-verification mechanism on upgraded servers.
run_root systemctl disable --now "${LEGACY_VERIFY_NAME}.timer" >/dev/null 2>&1 || true
run_root systemctl stop "${LEGACY_VERIFY_NAME}.service" >/dev/null 2>&1 || true
run_root rm -f "$LEGACY_VERIFY_SERVICE_FILE" "$LEGACY_VERIFY_TIMER_FILE"
run_root systemctl daemon-reload
run_root systemctl restart "$SERVICE_NAME"
run_root systemctl restart "$REPLENISH_SERVICE_NAME"
# The existing planning unit owns its database path and credentials. Reload
# its application code without replacing that server-specific configuration.
run_root systemctl restart "$PLANNING_SERVICE_NAME"

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

for attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 5 "$REPLENISH_LOCAL_URL/healthz" >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "ERROR 货品监控中心重启后 30 秒内未通过本机健康检查。" >&2
    run_root systemctl status "$REPLENISH_SERVICE_NAME" --no-pager || true
    run_root journalctl -u "$REPLENISH_SERVICE_NAME" -n 80 --no-pager || true
    exit 1
  fi
  sleep 1
done

run_root systemctl is-active --quiet "$SERVICE_NAME"
run_root systemctl is-active --quiet "$REPLENISH_SERVICE_NAME"
run_root systemctl is-active --quiet "$PLANNING_SERVICE_NAME"
CATALOG_DEPLOYMENT_CHECK_COUNT=256 CATALOG_EXPECTED_WORKERS=8 "$ROOT_DIR/scripts/verify_public_deployment.sh" "$LOCAL_URL"
PYTHON_BIN="$VENV_DIR/bin/python" REPLENISH_DEPLOYMENT_CHECK_COUNT=8 REPLENISH_EXPECTED_WORKERS=1 \
  "$ROOT_DIR/scripts/verify_replenishment_deployment.sh" "$REPLENISH_LOCAL_URL"
PYTHON_BIN="$VENV_DIR/bin/python" bash "$ROOT_DIR/scripts/verify_planning_deployment.sh" "$PLANNING_LOCAL_URL"

if [ -n "$PUBLIC_URL" ]; then
  CATALOG_DEPLOYMENT_CHECK_COUNT=256 CATALOG_EXPECTED_WORKERS=8 "$ROOT_DIR/scripts/verify_public_deployment.sh" "$PUBLIC_URL"
else
  echo "ERROR 未传入公网地址，无法完成发布后公网验收。" >&2
  echo "用法: $0 http://203.205.90.232:8765" >&2
  exit 1
fi

PYTHON_BIN="$VENV_DIR/bin/python" REPLENISH_DEPLOYMENT_CHECK_COUNT=16 REPLENISH_EXPECTED_WORKERS=1 \
  "$ROOT_DIR/scripts/verify_replenishment_deployment.sh" "$REPLENISH_PUBLIC_URL"
PYTHON_BIN="$VENV_DIR/bin/python" bash "$ROOT_DIR/scripts/verify_planning_deployment.sh" "$PLANNING_PUBLIC_URL"

echo "OK 正式服务已重启，藏宝阁、商品企划中心与货品监控中心均已加载本次源码。"
