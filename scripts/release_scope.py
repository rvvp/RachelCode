#!/usr/bin/env python3
"""Read-only monorepo release impact report. This tool NEVER deploys or pushes."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


APPS = ("catalog", "planning", "replenishment")
APP_PREFIXES = {
    "catalog_backend/": "catalog",
    "planning_center/": "planning",
    "replenishment_center/": "replenishment",
}
APP_FILES = {
    "app.py": "catalog",
    "catalog_wsgi.py": "catalog",
    "planning_app.py": "planning",
    "replenishment_app.py": "replenishment",
    "上新模板.xlsx": "catalog",
    "tests/test_planning.py": "planning",
    "tests/test_rotation.py": "planning",
    "tests/test_replenishment.py": "replenishment",
    "tests/test_app.py": "catalog",
}
# Existing source-level imports are consumers, NOT permission to deploy them.
SHARED_CONSUMERS = {
    "catalog_backend/__init__.py": APPS,
    "catalog_backend/release.py": APPS,
    "catalog_backend/concurrency.py": ("catalog", "planning"),
    "catalog_backend/uploads.py": ("catalog", "planning"),
}
RUNTIME_DIRECTORIES = {"data", "planning_data", "replenishment_data", "backups",
                       "logs", "exports", "outputs", "screenshots", "videos"}
RUNTIME_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
                    ".log", ".pem", ".key"}


def classify_path(path: str) -> dict:
    """Fail closed on unmapped files; never infer a deployment target."""
    parsed = PurePosixPath(path)
    parts = parsed.parts
    if not parts or parsed.is_absolute() or ".." in parts:
        return {"path": path, "kind": "unmapped", "owners": [], "consumers": []}
    if (any(part in RUNTIME_DIRECTORIES or part in {".git", ".ssh"} for part in parts)
            or any(part == ".env" or (part.startswith(".env.") and part != ".env.example")
                   for part in parts)
            or parsed.suffix in RUNTIME_SUFFIXES
            or path in {".release-commit", ".release-generation"}):
        kind, owners, consumers = "forbidden-runtime", [], []
    elif path in SHARED_CONSUMERS:
        kind, owners, consumers = "shared-source", ["catalog"], list(SHARED_CONSUMERS[path])
    elif path in APP_FILES:
        kind, owners, consumers = "app-source", [APP_FILES[path]], []
    elif (path in {"requirements.txt", ".env.example"}
          or path.startswith(("scripts/", "deploy/", ".gitea/", "launchd/"))
          or path in {"tests/test_release.py", "tests/test_release_boundaries.py", "tests/test_isolated_release.py"}):
        kind, owners, consumers = "release-infrastructure", [], list(APPS)
    elif path.endswith(".md") or path == ".gitignore":
        kind, owners, consumers = "documentation", [], []
    else:
        owner = next((app for prefix, app in APP_PREFIXES.items() if path.startswith(prefix)), None)
        kind, owners, consumers = ("app-source", [owner], []) if owner else ("unmapped", [], [])
    return {"path": path, "kind": kind, "owners": owners, "consumers": consumers}


def build_report(paths: list[str], requested_apps: list[str]) -> dict:
    requested = sorted(set(requested_apps))
    files = [classify_path(path) for path in sorted(set(paths))]
    owners = sorted({app for item in files for app in item["owners"]})
    consumers = sorted({app for item in files for app in item["consumers"]})
    issues = []
    if not requested:
        issues.append("explicit_application_target_required")
    if any(app not in APPS for app in requested):
        issues.append("unknown_application_target")
    if any(item["kind"] == "forbidden-runtime" for item in files):
        issues.append("runtime_or_sensitive_files_forbidden")
    if any(item["kind"] == "unmapped" for item in files):
        issues.append("unmapped_paths_require_review")
    if any(item["kind"] == "release-infrastructure" for item in files):
        issues.append("release_infrastructure_requires_isolation_review")
    if any(item["kind"] == "shared-source" for item in files):
        issues.append("shared_source_requires_consumer_compatibility_review")
    if set(owners) - set(requested):
        issues.append("changed_applications_outside_authorized_scope")
    return {
        "read_only": True,
        "requested_apps": requested,
        "changed_applications": owners,
        "compatibility_test_consumers": consumers,
        "must_not_restart": sorted(set(APPS) - set(requested)),
        "files": files,
        "issues": issues,
        "scope_review_passed": not issues,
        # No local impact report can certify the installed server dispatcher.
        "production_release_authorized": False,
        "server_isolation_status": "not_attested",
    }


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        text=True, check=True, timeout=15,
    )
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Last accepted release commit; no implicit HEAD^ default.")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--app", action="append", required=True, choices=APPS)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        base = git_output(root, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}").strip()
        commit = git_output(root, "rev-parse", "--verify", "--end-of-options", f"{args.commit}^{{commit}}").strip()
        git_output(root, "merge-base", "--is-ancestor", base, commit)
        # Treat renames as deletion + addition so both ownerships are checked.
        names = git_output(root, "diff", "--name-only", "--no-renames", "-z", base, commit).split("\0")
        report = build_report([name for name in names if name], args.app)
        report.update(base_commit=base, candidate_commit=commit)
        if git_output(root, "status", "--porcelain").strip():
            report["issues"].append("worktree_changes_not_in_candidate_commit")
            report["scope_review_passed"] = False
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"read_only": True, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["scope_review_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
