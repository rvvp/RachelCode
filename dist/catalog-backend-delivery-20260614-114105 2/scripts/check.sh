#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

if [ -n "${PYTHON_BIN:-}" ]; then
  :
elif [ -x "$ROOT_DIR/.venv/bin/python3" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "ERROR 未找到可用的 Python 解释器。请先安装 python3，或在项目目录创建 .venv，或设置 PYTHON_BIN。" >&2
  exit 1
fi

DB_PATH="${CATALOG_DB:-$ROOT_DIR/data/catalog.db}"
UPLOADS_DIR="${CATALOG_UPLOADS:-$ROOT_DIR/data/uploads}"
HEALTH_URL="${CATALOG_HEALTH_URL:-http://127.0.0.1:${CATALOG_PORT:-8765}/healthz}"

echo "检查项目目录: $ROOT_DIR"

if [ -f "$ROOT_DIR/.env" ]; then
  echo "OK .env 已存在"
else
  echo "WARN 未找到 .env"
fi

if [ -f "$DB_PATH" ]; then
  echo "OK 数据库存在: $DB_PATH"
else
  echo "WARN 数据库不存在: $DB_PATH"
fi

if [ -d "$UPLOADS_DIR" ]; then
  echo "OK 上传目录存在: $UPLOADS_DIR"
else
  echo "WARN 上传目录不存在: $UPLOADS_DIR"
fi

if [ -f "$ROOT_DIR/requirements.txt" ]; then
  echo "运行依赖检查"
  "$PYTHON_BIN" -m pip show openpyxl >/dev/null 2>&1 && echo "OK openpyxl 已安装" || echo "WARN openpyxl 未安装，Excel 导入导出不可用"
fi

echo "运行代码编译检查"
"$PYTHON_BIN" -m py_compile "$ROOT_DIR"/app.py "$ROOT_DIR"/catalog_backend/*.py "$ROOT_DIR"/tests/*.py

if command -v curl >/dev/null 2>&1; then
  echo "尝试访问健康检查: $HEALTH_URL"
  curl --fail --silent "$HEALTH_URL" || echo "WARN 健康检查暂未通过，可能服务尚未启动"
else
  echo "WARN 当前环境没有 curl，跳过健康检查请求"
fi

echo "自检完成"
