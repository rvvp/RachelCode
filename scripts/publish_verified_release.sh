#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_URL="${CATALOG_PUBLIC_URL:-http://203.205.90.232:8765}"
REPLENISH_PUBLIC_URL="${REPLENISH_PUBLIC_URL:-http://203.205.90.232:8877}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
WAIT_ATTEMPTS="${CATALOG_DEPLOY_WAIT_ATTEMPTS:-40}"
WAIT_SECONDS="${CATALOG_DEPLOY_WAIT_SECONDS:-15}"

fail() {
  echo "ERROR $*" >&2
  exit 1
}

[ -x "$PYTHON_BIN" ] || fail "未找到项目 Python: $PYTHON_BIN"
[ "$#" -gt 0 ] || fail "请传入本次修改对应的 unittest 名称。"
[ "$(git -C "$ROOT_DIR" branch --show-current)" = "main" ] || fail "只能从 main 分支发布。"
[ -z "$(git -C "$ROOT_DIR" status --porcelain)" ] || fail "工作区存在未提交修改，拒绝发布。"
git -C "$ROOT_DIR" remote get-url origin >/dev/null 2>&1 || fail "缺少 origin 远程仓库。"
git -C "$ROOT_DIR" remote get-url github >/dev/null 2>&1 || fail "缺少 github 备份仓库。"

release_commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"
release_generation="$(git -C "$ROOT_DIR" rev-list --count "$release_commit")"

echo "本机质量检查: $release_commit"
if git -C "$ROOT_DIR" rev-parse "${release_commit}^" >/dev/null 2>&1; then
  git -C "$ROOT_DIR" diff-tree --check "${release_commit}^" "$release_commit"
else
  git -C "$ROOT_DIR" diff-tree --check --root "$release_commit"
fi
"$PYTHON_BIN" -m py_compile \
  "$ROOT_DIR"/app.py \
  "$ROOT_DIR"/replenishment_app.py \
  "$ROOT_DIR"/catalog_backend/*.py \
  "$ROOT_DIR"/replenishment_center/*.py \
  "$ROOT_DIR"/tests/*.py
(
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m unittest \
    tests.test_release \
    tests.test_app.CatalogAppTests.test_healthz_returns_basic_runtime_status \
    "$@"
)

git -C "$ROOT_DIR" fetch origin main --quiet
if ! git -C "$ROOT_DIR" merge-base --is-ancestor origin/main "$release_commit"; then
  fail "origin/main 含有本机没有的新提交，请先同步后再发布。"
fi

echo "推送正式发布源: origin/main"
git -C "$ROOT_DIR" push origin "$release_commit:refs/heads/main"
origin_commit="$(git -C "$ROOT_DIR" ls-remote origin refs/heads/main | awk '{print $1}')"
[ "$origin_commit" = "$release_commit" ] || fail "origin/main 未指向本次提交。"

echo "等待公司服务器自动部署"
ready=0
for attempt in $(seq 1 "$WAIT_ATTEMPTS"); do
  if CATALOG_RELEASE_COMMIT="$release_commit" \
    CATALOG_RELEASE_GENERATION="$release_generation" \
    CATALOG_DEPLOYMENT_CHECK_COUNT=1 \
    CATALOG_EXPECTED_WORKERS=1 \
    "$ROOT_DIR/scripts/verify_public_deployment.sh" "$PUBLIC_URL" >/dev/null 2>&1 \
    && CATALOG_RELEASE_COMMIT="$release_commit" \
      CATALOG_RELEASE_GENERATION="$release_generation" \
      REPLENISH_DEPLOYMENT_CHECK_COUNT=1 \
      REPLENISH_EXPECTED_WORKERS=1 \
      "$ROOT_DIR/scripts/verify_replenishment_deployment.sh" "$REPLENISH_PUBLIC_URL" >/dev/null 2>&1; then
    ready=1
    echo "公网已切换到本次提交，第 $attempt 次检查通过。"
    break
  fi
  sleep "$WAIT_SECONDS"
done
[ "$ready" = "1" ] || fail "公网未在等待时间内切换到本次提交，GitHub 不会更新。"

CATALOG_RELEASE_COMMIT="$release_commit" \
  CATALOG_RELEASE_GENERATION="$release_generation" \
  CATALOG_DEPLOYMENT_CHECK_COUNT=256 \
  CATALOG_EXPECTED_WORKERS=8 \
  "$ROOT_DIR/scripts/verify_public_deployment.sh" "$PUBLIC_URL"
CATALOG_RELEASE_COMMIT="$release_commit" \
  CATALOG_RELEASE_GENERATION="$release_generation" \
  REPLENISH_DEPLOYMENT_CHECK_COUNT=16 \
  REPLENISH_EXPECTED_WORKERS=1 \
  "$ROOT_DIR/scripts/verify_replenishment_deployment.sh" "$REPLENISH_PUBLIC_URL"

origin_commit="$(git -C "$ROOT_DIR" ls-remote origin refs/heads/main | awk '{print $1}')"
[ "$origin_commit" = "$release_commit" ] || fail "验收期间 origin/main 已变化，停止 GitHub 备份。"

git -C "$ROOT_DIR" fetch github main --quiet
github_commit="$(git -C "$ROOT_DIR" rev-parse refs/remotes/github/main)"
if ! git -C "$ROOT_DIR" merge-base --is-ancestor "$github_commit" "$release_commit"; then
  fail "github/main 与本次提交不是快进关系，拒绝覆盖备份历史。"
fi

echo "公网验收通过，备份同一提交到 github/main"
git -C "$ROOT_DIR" push github "$release_commit:refs/heads/main"
github_remote_commit="$(git -C "$ROOT_DIR" ls-remote github refs/heads/main | awk '{print $1}')"
[ "$github_remote_commit" = "$release_commit" ] || fail "github/main 未指向已验收提交。"

echo "OK 发布闭环完成: origin/main、藏宝阁、货品监控中心、github/main 均为 $release_commit"
