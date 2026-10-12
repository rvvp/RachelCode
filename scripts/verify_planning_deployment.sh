#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${1:-${PLANNING_PUBLIC_URL:-http://203.205.90.232:8785}}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
CHECK_COUNT="${PLANNING_DEPLOYMENT_CHECK_COUNT:-8}"

cd "$ROOT_DIR"
"$PYTHON_BIN" - "${BASE_URL%/}" "$CHECK_COUNT" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

from planning_center.release import (
    PLANNING_RELEASE_COMMIT,
    PLANNING_RELEASE_GENERATION,
    PLANNING_SOURCE_FINGERPRINT,
)

base_url, count = sys.argv[1:]
if PLANNING_RELEASE_COMMIT == "unknown" or PLANNING_RELEASE_GENERATION <= 0:
    raise SystemExit("ERROR 无法确定商品企划中心待发布版本。")
for attempt in range(1, int(count) + 1):
    request = Request(
        f"{base_url}/healthz?deployment_check={PLANNING_RELEASE_COMMIT}-{attempt}",
        headers={"Cache-Control": "no-cache", "Connection": "close"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    expected = {
        "status": "ok",
        "application": "planning_center",
        "db_exists": True,
        "release_commit": PLANNING_RELEASE_COMMIT,
        "release_generation": PLANNING_RELEASE_GENERATION,
        "source_fingerprint": PLANNING_SOURCE_FINGERPRINT,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise SystemExit(f"ERROR 商品企划中心第 {attempt} 次验证失败: {key}={payload.get(key)!r}，期望 {value!r}")
    if not isinstance(payload.get("worker_pid"), int) or payload["worker_pid"] <= 0:
        raise SystemExit("ERROR 商品企划中心未返回有效进程编号。")
print(f"OK 商品企划中心连续 {count} 次验证通过: {PLANNING_RELEASE_COMMIT} / {PLANNING_SOURCE_FINGERPRINT}")
PY
