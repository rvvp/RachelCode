#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
PACKAGE_NAME="catalog-backend-delivery"
TIMESTAMP="$(date +"%Y%m%d-%H%M%S")"
STAGING_DIR="$DIST_DIR/${PACKAGE_NAME}-${TIMESTAMP}"
ARCHIVE_FILE="$DIST_DIR/${PACKAGE_NAME}-${TIMESTAMP}.tar.gz"
RELEASE_COMMIT="${CATALOG_RELEASE_COMMIT:-$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)}"
RELEASE_GENERATION="${CATALOG_RELEASE_GENERATION:-$(git -C "$ROOT_DIR" rev-list --count "$RELEASE_COMMIT" 2>/dev/null || true)}"

if ! [[ "$RELEASE_COMMIT" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
  echo "ERROR 无法确定交付包对应的 Git 提交号。" >&2
  exit 1
fi
if ! [[ "$RELEASE_GENERATION" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR 无法确定交付包对应的版本序号。" >&2
  exit 1
fi

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
copy_path "catalog_wsgi.py"
copy_path "catalog_backend"
copy_path "scripts"
copy_path "deploy"
copy_path "launchd"
copy_path "README.md"
copy_path "DEPLOYMENT_CHECKLIST.md"
copy_path "requirements.txt"
copy_path ".env.example"
copy_path "上新模板.xlsx"
printf '%s\n' "$RELEASE_COMMIT" | tr '[:upper:]' '[:lower:]' > "$STAGING_DIR/.release-commit"
printf '%s\n' "$RELEASE_GENERATION" > "$STAGING_DIR/.release-generation"

find "$STAGING_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGING_DIR" -name '*.pyc' -type f -delete

tar -C "$DIST_DIR" -czf "$ARCHIVE_FILE" "$(basename "$STAGING_DIR")"

echo "交付包已生成: $ARCHIVE_FILE"
