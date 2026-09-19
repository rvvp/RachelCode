#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${1:-${CATALOG_PUBLIC_URL:-}}"
CHECK_COUNT="${CATALOG_DEPLOYMENT_CHECK_COUNT:-24}"
EXPECTED_WORKERS="${CATALOG_EXPECTED_WORKERS:-1}"

if [ -z "$BASE_URL" ]; then
  echo "ERROR 请传入公网根地址，例如: $0 http://203.205.90.232:8765" >&2
  exit 1
fi

BASE_URL="${BASE_URL%/}"
EXPECTED_BUILD="$(sed -n 's/^CATALOG_BUILD_VERSION = "\([^"]*\)"/\1/p' "$ROOT_DIR/catalog_backend/web.py" | head -n 1)"
EXPECTED_SOURCE="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_SOURCE_FINGERPRINT"])')"
EXPECTED_COMMIT="$(cd "$ROOT_DIR" && python3 -c 'import runpy; print(runpy.run_path("catalog_backend/release.py")["CATALOG_RELEASE_COMMIT"])')"

if [ -z "$EXPECTED_BUILD" ] || [ -z "$EXPECTED_SOURCE" ] || [ "$EXPECTED_COMMIT" = "unknown" ]; then
  echo "ERROR 无法计算待发布代码的版本、提交号或源码指纹。" >&2
  exit 1
fi

echo "期望构建: $EXPECTED_BUILD"
echo "期望提交: $EXPECTED_COMMIT"
echo "期望源码指纹: $EXPECTED_SOURCE"

workers_file="$(mktemp)"
payload_file=""
headers_file=""
cleanup() {
  rm -f "$workers_file" "${payload_file:-}" "${headers_file:-}"
}
trap cleanup EXIT

for attempt in $(seq 1 "$CHECK_COUNT"); do
  payload_file="$(mktemp)"
  headers_file="$(mktemp)"
  http_code="$(curl --silent --show-error --max-time 15 --connect-timeout 5 \
    --header 'Cache-Control: no-cache' --header 'Connection: close' \
    --dump-header "$headers_file" --output "$payload_file" --write-out '%{http_code}' \
    "$BASE_URL/healthz?deployment_check=$(date +%s)-$attempt")"
  python3 - "$payload_file" "$http_code" "$EXPECTED_BUILD" "$EXPECTED_COMMIT" "$EXPECTED_SOURCE" "$attempt" "$workers_file" <<'PY'
import json
import sys

payload_path, http_code, expected_build, expected_commit, expected_source, attempt, workers_path = sys.argv[1:]
with open(payload_path, "r", encoding="utf-8") as source:
    payload = json.load(source)
errors = []
if http_code != "200":
    errors.append(f"HTTP {http_code}")
if payload.get("build_version") != expected_build:
    errors.append(f"构建号为 {payload.get('build_version')!r}")
if payload.get("release_commit") != expected_commit:
    errors.append(f"提交号为 {payload.get('release_commit')!r}")
if payload.get("source_fingerprint") != expected_source:
    errors.append(f"源码指纹为 {payload.get('source_fingerprint')!r}")
if payload.get("runtime_mode") != "production":
    errors.append(f"运行模式为 {payload.get('runtime_mode')!r}")
if not payload.get("production_runtime_ready"):
    errors.append("生产运行时未就绪")
if "gunicorn" not in str(payload.get("server_software") or "").lower():
    errors.append(f"服务器为 {payload.get('server_software')!r}，不是 Gunicorn")
worker_pid = payload.get("worker_pid")
if not isinstance(worker_pid, int) or worker_pid <= 0:
    errors.append(f"工作进程编号为 {worker_pid!r}")
if errors:
    raise SystemExit(f"ERROR 第 {attempt} 次公网验证失败: " + "；".join(errors))
with open(workers_path, "a", encoding="utf-8") as output:
    output.write(f"{worker_pid}\n")
if int(attempt) <= 3 or int(attempt) % 32 == 0:
    print(
        f"OK 第 {attempt} 次: {payload['build_version']} / "
        f"{payload['release_commit'][:12]} / "
        f"{payload['source_fingerprint']} / {payload['server_software']} / "
        f"启动于 {payload['process_started_at']}"
    )
PY
  rm -f "$payload_file" "$headers_file"
  payload_file=""
  headers_file=""
done

observed_workers="$(sort -u "$workers_file" | wc -l | tr -d ' ')"
if [ "$observed_workers" -lt "$EXPECTED_WORKERS" ]; then
  echo "ERROR 只抽查到 $observed_workers 个 Gunicorn 工作进程，期望至少 $EXPECTED_WORKERS 个。" >&2
  exit 1
fi

echo "OK 连续 $CHECK_COUNT 次均已加载本次源码，已覆盖 $observed_workers 个 Gunicorn 工作进程。"
