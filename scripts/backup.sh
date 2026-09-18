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
  echo "ERROR 未找到可用的 Python 解释器，无法创建 SQLite 一致性备份。" >&2
  exit 1
fi

TIMESTAMP="$(date +"%Y%m%d-%H%M%S")"
BACKUP_DIR="$ROOT_DIR/backups/$TIMESTAMP"
DB_PATH="${CATALOG_DB:-$ROOT_DIR/data/catalog.db}"
UPLOADS_DIR="${CATALOG_UPLOADS:-$ROOT_DIR/data/uploads}"
BACKUP_KEEP="${CATALOG_BACKUP_KEEP:-14}"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
  "$PYTHON_BIN" - "$DB_PATH" "$BACKUP_DIR/catalog.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
fi

if [ -d "$UPLOADS_DIR" ]; then
  mkdir -p "$BACKUP_DIR/uploads"
  cp -R "$UPLOADS_DIR"/. "$BACKUP_DIR/uploads/" 2>/dev/null || true
fi

if [ "$BACKUP_KEEP" -gt 0 ] 2>/dev/null; then
  EXISTING_BACKUPS=("${(@f)$(find "$ROOT_DIR/backups" -mindepth 1 -maxdepth 1 -type d | sort)}")
  if [ "${#EXISTING_BACKUPS[@]}" -gt "$BACKUP_KEEP" ]; then
    REMOVE_COUNT=$((${#EXISTING_BACKUPS[@]} - BACKUP_KEEP))
    for OLD_BACKUP in "${EXISTING_BACKUPS[@]:0:$REMOVE_COUNT}"; do
      rm -rf "$OLD_BACKUP"
    done
  fi
fi

echo "备份完成: $BACKUP_DIR"
