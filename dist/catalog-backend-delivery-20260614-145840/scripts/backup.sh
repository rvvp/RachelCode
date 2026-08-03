#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

TIMESTAMP="$(date +"%Y%m%d-%H%M%S")"
BACKUP_DIR="$ROOT_DIR/backups/$TIMESTAMP"
DB_PATH="${CATALOG_DB:-$ROOT_DIR/data/catalog.db}"
UPLOADS_DIR="${CATALOG_UPLOADS:-$ROOT_DIR/data/uploads}"
BACKUP_KEEP="${CATALOG_BACKUP_KEEP:-14}"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
  cp "$DB_PATH" "$BACKUP_DIR/catalog.db"
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
