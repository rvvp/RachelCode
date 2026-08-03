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
  echo "未找到可用的 Python 解释器。请先安装 python3，或在项目目录创建 .venv，或设置 PYTHON_BIN。" >&2
  exit 1
fi

if [ ! -f "$ROOT_DIR/.env" ] && [ -f "$ROOT_DIR/.env.example" ]; then
  echo "未找到 .env，当前可先复制 .env.example 为 .env 后再启动。"
fi

exec "$PYTHON_BIN" "$ROOT_DIR/app.py" "$@"
