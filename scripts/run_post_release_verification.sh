#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_URL="${1:-${CATALOG_PUBLIC_URL:-}}"
STATE_PATH="${CATALOG_POST_RELEASE_STATE:-$ROOT_DIR/.post-release-verification.json}"
CHECK_COUNT="${CATALOG_DEPLOYMENT_CHECK_COUNT:-256}"
EXPECTED_WORKERS="${CATALOG_EXPECTED_WORKERS:-8}"

if [ -z "$PUBLIC_URL" ]; then
  echo "ERROR 未传入公网地址，无法执行发布后复验。" >&2
  exit 1
fi

EXPECTED_BUILD="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"
EXPECTED_SOURCE="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_SOURCE_FINGERPRINT"])')"
EXPECTED_COMMIT="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_COMMIT"])')"
EXPECTED_GENERATION="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_GENERATION"])')"

set +e
verification_output="$(
  CATALOG_DEPLOYMENT_CHECK_COUNT="$CHECK_COUNT" \
  CATALOG_EXPECTED_WORKERS="$EXPECTED_WORKERS" \
  "$ROOT_DIR/scripts/verify_public_deployment.sh" "$PUBLIC_URL" 2>&1
)"
verification_status=$?
set -e
printf '%s\n' "$verification_output"

if [ "$verification_status" -eq 0 ]; then
  result_status="passed"
else
  result_status="failed"
fi
verified_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

mkdir -p "$(dirname "$STATE_PATH")"
python3 - "$STATE_PATH" "$result_status" "$verified_at" "$EXPECTED_COMMIT" \
  "$EXPECTED_GENERATION" "$EXPECTED_BUILD" "$EXPECTED_SOURCE" "$CHECK_COUNT" "$EXPECTED_WORKERS" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    state_path,
    status,
    verified_at,
    release_commit,
    release_generation,
    build_version,
    source_fingerprint,
    check_count,
    expected_workers,
) = sys.argv[1:]
path = Path(state_path)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
payload = {
    "status": status,
    "verified_at": verified_at,
    "release_commit": release_commit,
    "release_generation": int(release_generation),
    "build_version": build_version,
    "source_fingerprint": source_fingerprint,
    "check_count": int(check_count),
    "expected_workers": int(expected_workers),
}
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY

exit "$verification_status"
