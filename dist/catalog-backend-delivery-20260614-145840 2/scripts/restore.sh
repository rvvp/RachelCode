#!/bin/zsh
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "用法: scripts/restore.sh <备份目录>"
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

BACKUP_DIR="$1"
DB_PATH="${CATALOG_DB:-$ROOT_DIR/data/catalog.db}"
UPLOADS_DIR="${CATALOG_UPLOADS:-$ROOT_DIR/data/uploads}"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "备份目录不存在: $BACKUP_DIR"
  exit 1
fi

mkdir -p "$(dirname "$DB_PATH")"
mkdir -p "$UPLOADS_DIR"

if [ -f "$BACKUP_DIR/catalog.db" ]; then
  cp "$BACKUP_DIR/catalog.db" "$DB_PATH"
fi

if [ -d "$BACKUP_DIR/uploads" ]; then
  rm -rf "$UPLOADS_DIR"
  mkdir -p "$UPLOADS_DIR"
  cp -R "$BACKUP_DIR/uploads"/. "$UPLOADS_DIR/" 2>/dev/null || true
fi

echo "恢复完成: $BACKUP_DIR"
