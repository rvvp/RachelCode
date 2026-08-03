#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

PYTHON_BIN=${PYTHON_BIN:-"$ROOT_DIR/.venv/bin/python"}
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=${PYTHON_BIN_FALLBACK:-python3}
fi

exec "$PYTHON_BIN" replenishment_app.py "$@"
