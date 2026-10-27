#!/bin/bash
set -euo pipefail
# Explicit --app is mandatory. Server isolation proof comes from origin commit
# statuses BEFORE pushing origin; legacy positional use is rejected. No SSH.
if [ "${1:-}" != "--app" ]; then
  echo 'ERROR 旧的跨系统发布入口已禁用。用法: publish_verified_release.sh --app planning tests.test_planning' >&2
  exit 78
fi
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/publish_isolated_release.py" "$@"
