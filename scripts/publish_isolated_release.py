#!/usr/bin/env python3
"""Test -> attest installed isolation -> origin -> target acceptance -> GitHub."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from isolated_release import (APPLICATIONS, APPS, PACKAGES, ReleaseError, VERSION,
                              validate_plan, validate_code_only_paths, health)


ROOT = Path(__file__).resolve().parent.parent


def run(*args, timeout=180):
    result = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ReleaseError(str(args[0]) + " failed (exit " + str(result.returncode) + ")")
    return result.stdout.strip()


def git(*args):
    return run("git", *args)


def attestation():
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"]
    if os.environ.get("SSH_CONTROL_PATH"):
        command += ["-S", os.environ["SSH_CONTROL_PATH"]]
    command += ["root@203.205.90.232", "/usr/bin/python3 /opt/rachelcode/automation/isolated-v1/isolated_release.py --attest"]
    proof = json.loads(run(*command, timeout=30))
    if proof.get("version") != VERSION:
        raise ReleaseError("server isolation version mismatch")
    for name in ("isolated_release.py", "release_scope.py"):
        digest = hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        if proof.get("installed_hashes", {}).get(name) != digest:
            raise ReleaseError("server isolation not installed or source differs: " + name)
    return proof


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
        proof = attestation()  # BEFORE any origin push.
        live = health(args.app, public=True)
        base = live.get("release_commit")
        paths = git("diff", "--name-only", "--no-renames", base, sha).splitlines()
        validate_plan(plan, git("show", "-s", "--format=%B", sha), paths, base, proof["enabled_apps"])
        validate_code_only_paths(paths)
        protected = {app: identity for app, identity in proof["services"].items() if app != args.app}
        git("diff-tree", "--check", sha + "^", sha)
        print("运行本地测试，目标：" + args.app, flush=True)
        compile_paths = [path for path in PACKAGES[args.app] if (ROOT / path).is_dir() or path.endswith(".py")]
        subprocess.run([sys.executable, "-m", "compileall", "-q", *compile_paths], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "unittest", "tests.test_release_boundaries",
                        "tests.test_isolated_release", "tests.test_release", *args.tests], cwd=ROOT, check=True)
        for remote in ("origin", "github"):
            git("fetch", "--quiet", remote, "main")
            git("merge-base", "--is-ancestor", remote + "/main", sha)
        # Never advance GitHub as a workaround for failed deployment.
        print("推送 origin/main；仅激活 " + args.app, flush=True)
        git("push", "origin", sha + ":refs/heads/main")
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("origin commit changed")
        sys.path.insert(0, str(ROOT))
        module = importlib.import_module(APPLICATIONS[args.app] + ".release")
        prefix = {"planning": "PLANNING", "catalog": "CATALOG", "replenishment": "REPLENISHMENT"}[args.app]
        expected = {"release_commit": sha, "release_generation": generation,
                    "source_fingerprint": getattr(module, prefix + "_SOURCE_FINGERPRINT")}
        deadline = time.monotonic() + 600
        while True:
            try:
                verify_payload(args.app, health(args.app, public=True), expected)
                break
            except (OSError, ValueError, ReleaseError):
                if time.monotonic() >= deadline:
                    raise ReleaseError("target public release timed out; GitHub unchanged")
                time.sleep(10)
        after = attestation()
        if {app: item for app, item in after["services"].items() if app != args.app} != protected:
            raise ReleaseError("non-target service identity changed; GitHub unchanged")
        # Server acceptance is required in addition to a single public response.
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if os.environ.get("SSH_CONTROL_PATH"):
            ssh += ["-S", os.environ["SSH_CONTROL_PATH"]]
        ssh += ["root@203.205.90.232", "test -f /opt/rachelcode/" + args.app + "/state.json && "
                "/usr/bin/python3 -c 'import json; print(json.load(open(\"/opt/rachelcode/" + args.app + "/state.json\"))[\"accepted_commit\"])'"]
        accepted = ""
        for _ in range(10):
            try:
                accepted = run(*ssh, timeout=30)
            except ReleaseError:
                accepted = ""
            if accepted == sha:
                break
            time.sleep(2)
        if accepted != sha:
            raise ReleaseError("server has not accepted target release")
        # Multiple public samples, target only; other sites keep accepted versions.
        for _ in range(3):
            verify_payload(args.app, health(args.app, public=True), expected)
            time.sleep(1)
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("origin advanced during acceptance")
        print("目标公网验收通过；备份同一提交到 github/main", flush=True)
        git("push", "github", sha + ":refs/heads/main")
        if git("ls-remote", "github", "refs/heads/main").split()[0] != sha:
            raise ReleaseError("GitHub backup commit mismatch")
        print("OK " + args.app + ": origin、目标公网、github = " + sha, flush=True)
    except (OSError, ValueError, subprocess.SubprocessError, ReleaseError) as exc:
        print("RELEASE_BLOCKED " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
