#!/usr/bin/env python3
"""Test -> origin readiness -> origin push -> server receipt/public -> GitHub.

Routine releases never SSH to the server. Its fixed dispatcher performs all
isolation checks and reports authenticated, exact-commit receipts to origin.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time

from isolated_release import (APPLICATIONS, APPS, PACKAGES, ReleaseError,
                              validate_plan, review_paths, health)
from release_status import (OriginStatus, StatusError, manifest, manifest_digest,
                            readiness, verify_receipt, wait_receipt)


ROOT = Path(__file__).resolve().parent.parent


def run(*args, timeout=180):
    result = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ReleaseError(str(args[0]) + " failed (exit " + str(result.returncode) + ")")
    return result.stdout.strip()


def git(*args):
    return run("git", *args)


def verify_payload(app, payload, expected):
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ReleaseError("public target mismatch: " + key)
    if ((app != "catalog" and payload.get("application") != APPLICATIONS[app])
            or payload.get("status") != "ok" or not payload.get("db_exists")):
        raise ReleaseError("public target is not healthy")
    if not isinstance(payload.get("worker_pid"), int) or payload["worker_pid"] <= 0:
        raise ReleaseError("target worker missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, choices=APPS)
    parser.add_argument("tests", nargs="+", help="Relevant unittest names; no cross-site public checks.")
    args = parser.parse_args()
    try:
        if git("branch", "--show-current") != "main" or git("status", "--porcelain"):
            raise ReleaseError("release requires a clean committed main branch")
        sha = git("rev-parse", "HEAD")
        generation = int(git("rev-list", "--count", sha))
        plan = json.loads((ROOT / "deploy/release-plan.json").read_text())
        if plan.get("application") != args.app:
            raise ReleaseError("--app differs from reviewed release plan")
        print("运行本地测试，目标：" + args.app, flush=True)
        compile_paths = [path for path in PACKAGES[args.app] if (ROOT / path).is_dir() or path.endswith(".py")]
        subprocess.run([sys.executable, "-m", "compileall", "-q", *compile_paths], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "unittest", "tests.test_release_boundaries",
                        "tests.test_isolated_release", "tests.test_release_status",
                        "tests.test_isolated_migration", "tests.test_release", *args.tests], cwd=ROOT, check=True)
        for remote in ("origin", "github"):
            git("fetch", "--quiet", remote, "main")
            git("merge-base", "--is-ancestor", remote + "/main", sha)
        origin = git("ls-remote", "origin", "refs/heads/main").split()[0]
        # This proof is posted by the installed server dispatcher after checking
        # its root-pinned manifest. No root credential or server connection here.
        client = OriginStatus()
        digest = manifest_digest(manifest(ROOT / "scripts"))
        proof = readiness(client, origin, digest, args.app)  # BEFORE origin push.
        live = health(args.app, public=True)
        base = plan["base_commit"] if origin == sha else live.get("release_commit")
        paths = review_paths(plan, base, sha, git)
        validate_plan(plan, git("show", "-s", "--format=%B", sha), paths, base, proof["apps"].split(","))
        git("diff", "--check", plan.get("repository_base_commit", base), sha)
        # Never advance GitHub as a workaround for failed deployment.
        print("推送 origin/main；仅激活 " + args.app, flush=True)
        if ("repository_base_commit" in plan
                and origin not in {plan["repository_base_commit"], sha}):
            raise ReleaseError("origin differs from the reviewed repository base")
        git("push", "origin", sha + ":refs/heads/main")
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("origin commit changed")
        sys.path.insert(0, str(ROOT))
        module = importlib.import_module(APPLICATIONS[args.app] + ".release")
        prefix = {"planning": "PLANNING", "catalog": "CATALOG", "replenishment": "REPLENISHMENT"}[args.app]
        expected = {"release_commit": sha, "release_generation": generation,
                    "source_fingerprint": getattr(module, prefix + "_SOURCE_FINGERPRINT")}
        wait_receipt(client, args.app, sha, expected, digest)
        deadline = time.monotonic() + 600
        while True:
            try:
                verify_payload(args.app, health(args.app, public=True), expected)
                break
            except (OSError, ValueError, ReleaseError):
                if time.monotonic() >= deadline:
                    raise ReleaseError("target public release timed out; GitHub unchanged")
                time.sleep(10)
        # Multiple public samples, target only; other sites keep accepted versions.
        for _ in range(3):
            verify_payload(args.app, health(args.app, public=True), expected)
            time.sleep(1)
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("origin advanced during acceptance")
        verify_receipt(client.latest(sha, "isolated-release/" + args.app), sha, expected, digest)
        print("目标公网验收通过；备份同一提交到 github/main", flush=True)
        git("push", "github", sha + ":refs/heads/main")
        if git("ls-remote", "github", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("GitHub backup commit mismatch")
        print("OK " + args.app + ": origin、目标公网、github = " + sha, flush=True)
    except (OSError, ValueError, subprocess.SubprocessError, ReleaseError, StatusError) as exc:
        print("RELEASE_BLOCKED " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
