#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
PACKAGE_NAME="catalog-backend-delivery"
TIMESTAMP="$(date +"%Y%m%d-%H%M%S")"
STAGING_DIR="$DIST_DIR/${PACKAGE_NAME}-${TIMESTAMP}"
ARCHIVE_FILE="$DIST_DIR/${PACKAGE_NAME}-${TIMESTAMP}.tar.gz"

mkdir -p "$DIST_DIR"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

copy_path() {
  local source_path="$1"
  if [ -e "$ROOT_DIR/$source_path" ]; then
    mkdir -p "$(dirname "$STAGING_DIR/$source_path")"
    cp -R "$ROOT_DIR/$source_path" "$STAGING_DIR/$source_path"
  fi
}

copy_path "app.py"
copy_path "catalog_backend"
copy_path "scripts"
copy_path "launchd"
copy_path "README.md"
copy_path "DEPLOYMENT_CHECKLIST.md"
copy_path "requirements.txt"
copy_path ".env.example"
copy_path "上新模板.xlsx"

find "$STAGING_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGING_DIR" -name '*.pyc' -type f -delete

tar -C "$DIST_DIR" -czf "$ARCHIVE_FILE" "$(basename "$STAGING_DIR")"

echo "交付包已生成: $ARCHIVE_FILE"
