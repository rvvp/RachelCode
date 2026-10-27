#!/usr/bin/env python3
"""Installed, reviewed server dispatcher. Python 3.9+, stdlib only.

This file is installed OUTSIDE application releases. A git push cannot replace
the dispatcher, enable a new application, update nginx, or restore a database.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

from release_scope import APPS, classify_path


VERSION = "isolated-v1"
CONFIG = Path("/etc/rachelcode/isolated-release.json")
AUTOMATION = Path("/opt/rachelcode/automation")
ROOT = Path("/opt/rachelcode")
SYSTEMD_DIR = Path("/etc/systemd/system")
SERVICES = {app: "rachel-" + app for app in APPS}
PORTS = {"catalog": 8765, "planning": 8785, "replenishment": 8877}
APPLICATIONS = {"catalog": "catalog_backend", "planning": "planning_center",
                "replenishment": "replenishment_center"}
PACKAGES = {
    "catalog": ["app.py", "catalog_wsgi.py", "catalog_backend", "requirements.txt", "scripts", "deploy"],
    "planning": ["planning_app.py", "planning_center", "catalog_backend", "requirements.txt"],
    "replenishment": ["replenishment_app.py", "replenishment_center", "catalog_backend",
                      "requirements.txt", "scripts", "deploy"],
}


class ReleaseError(RuntimeError):
    pass


def run(*args, cwd=None, timeout=180, env=None, binary=False):
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, capture_output=True,
                            text=not binary, timeout=timeout)
    if result.returncode:
        # Never echo environment/configuration or credential-bearing git output.
        raise ReleaseError("command failed: " + str(args[0]) + " (exit " + str(result.returncode) + ")")
    return result.stdout if binary else result.stdout.strip()


def git(*args, binary=False):
    return run("git", "--git-dir", AUTOMATION / "repository.git", *args, binary=binary)


def valid_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ReleaseError("invalid exact commit")
    return value


def validate_plan(plan, trailers, paths, current_commit, enabled):
    if plan.get("schema") != 1 or plan.get("application") not in APPS:
        raise ReleaseError("explicit single application plan required")
    app = plan["application"]
    if app not in enabled:
        raise ReleaseError("application not enabled for isolated deployment: " + app)
    if trailers.splitlines().count("Release-App: " + app) != 1 or sum(
            line.startswith("Release-App:") for line in trailers.splitlines()) != 1:
        raise ReleaseError("commit must explicitly authorize this application's release")
    if valid_sha(plan.get("base_commit")) != current_commit:
        raise ReleaseError("release base is not the application's currently accepted release")
    reviewed = plan.get("reviewed_paths", [])
    if not isinstance(reviewed, list) or any(not isinstance(x, str) for x in reviewed):
        raise ReleaseError("invalid reviewed path list")
    for path in paths:
        item = classify_path(path)
        kind = item["kind"]
        if kind in {"forbidden-runtime", "unmapped"}:
            raise ReleaseError("forbidden or unreviewed file: " + path)
        if kind == "app-source" and item["owners"] != [app]:
            raise ReleaseError("another application's source changed: " + path)
        if kind == "shared-source" and (app not in item["consumers"] or path not in reviewed):
            raise ReleaseError("shared dependency snapshot needs explicit review: " + path)
        if kind == "release-infrastructure" and path not in reviewed:
            raise ReleaseError("infrastructure change needs explicit review: " + path)
    # Schema/data migrations need a separate reviewed backup/migration procedure.
    # This first dispatcher intentionally does not run them against live data.
    if plan.get("database_migration") is not False:
        raise ReleaseError("this code-only release path does not authorize database migrations")
    return app


def check_no_schema_change(app, base, sha):
    paths = git("diff", "--name-only", base, sha).splitlines()
    validate_code_only_paths(paths)


def validate_code_only_paths(paths):
    sensitive = {"catalog_backend/db.py", "planning_center/db.py", "planning_center/rotation.py",
                 "replenishment_center/db.py"}
    if set(paths) & sensitive:
        raise ReleaseError("schema/data initialization changed; separate migration review required")
    if "requirements.txt" in paths:
        raise ReleaseError("dependency constraints changed; review the target's private lock before release")


def safe_extract(blob, destination):
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ReleaseError("unsafe archive entry")
            if classify_path(member.name)["kind"] == "forbidden-runtime":
                raise ReleaseError("runtime data in source package")
        archive.extractall(destination)


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(path):
    with open(path, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def health(app, public=False):
    host = "203.205.90.232" if public else "127.0.0.1"
    request = Request("http://%s:%s/healthz?isolated=%s" % (host, PORTS[app], time.time_ns()),
                      headers={"Cache-Control": "no-cache", "Connection": "close"})
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def identity(app):
    values = run("systemctl", "show", SERVICES[app], "--property=MainPID,ActiveState,ExecMainStartTimestampMonotonic,WorkingDirectory", "--no-pager")
    result = dict(line.split("=", 1) for line in values.splitlines() if "=" in line)
    if result.get("ActiveState") != "active" or int(result.get("MainPID", "0")) <= 0:
        raise ReleaseError("service is not active: " + app)
    return result


def assert_protected(before, target):
    after = {app: identity(app) for app in APPS if app != target}
    if before != after:
        raise ReleaseError("non-target service identity changed; no action on that service is permitted")
    return after


def service_command(app, app_dir, python, workers=1):
    # No arbitrary service names or commands from release metadata.
    if app == "planning":
        return [str(python), str(app_dir / "planning_app.py"), "--host", "0.0.0.0", "--port", "8785",
                "--db", "/data/rachelcode/planning/planning.db", "--no-seed-demo"]
    if app == "replenishment":
        return [str(python), str(app_dir / "replenishment_app.py"), "--host", "0.0.0.0", "--port", "8877",
                "--db", "/data/rachelcode/replenishment/replenishment.db", "--no-seed-demo", "--scheduler"]
    return [str(python.parent / "gunicorn"), "--chdir", str(app_dir), "--bind", "0.0.0.0:8765",
            "--workers", str(workers), "--threads", "4", "--worker-class", "gthread", "--timeout", "1800",
            "--graceful-timeout", "30", "--keep-alive", "5", "--max-requests", "1000",
            "--max-requests-jitter", "100", "--worker-tmp-dir", "/dev/shm", "catalog_wsgi:application"]


def dropin_text(app, app_dir, python, sha, generation, workers=1):
    command = " ".join(service_command(app, app_dir, python, workers))
    return ("[Service]\nWorkingDirectory=%s\nExecStart=\nExecStart=%s\nEnvironmentFile=\n"
            "EnvironmentFile=/etc/rachelcode/%s.env\n"
            "Environment=CATALOG_RELEASE_COMMIT=%s CATALOG_RELEASE_GENERATION=%s\n" %
            (app_dir, command, app, sha, generation))


def activation(app, dropin, content, verify):
    """Only this function can restart a business service; rollback is code-only."""
    old = dropin.read_bytes() if dropin.exists() else None
    atomic_write(dropin, content)
    try:
        run("systemctl", "daemon-reload")
        run("systemctl", "restart", SERVICES[app])
        return verify()
    except BaseException:
        if old is None:
            dropin.unlink(missing_ok=True)
        else:
            atomic_write(dropin, old)
        run("systemctl", "daemon-reload")
        run("systemctl", "restart", SERVICES[app])
        raise


def verify_target(app, expected, protected, timeout=120):
    deadline = time.monotonic() + timeout
    seen = {False: set(), True: set()}
    last_workers = set()
    last_error = "startup pending"
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            live = identity(app)
            master = int(live["MainPID"])
            if app == "catalog":
                children = Path("/proc/%s/task/%s/children" % (master, master)).read_text().split()
                wanted = {int(x) for x in children}
                if len(wanted) != expected["workers"]:
                    raise ReleaseError("target workers are still starting")
            else:
                wanted = {master}
            if wanted != last_workers:
                seen = {False: set(), True: set()}
                consecutive = 0
                last_workers = wanted
            for public in (False, True):
                payload = health(app, public)
                for key in ("release_commit", "release_generation", "source_fingerprint"):
                    if payload.get(key) != expected[key]:
                        raise ReleaseError("target health mismatch: " + key)
                if payload.get("status") != "ok" or not payload.get("db_exists"):
                    raise ReleaseError("target not healthy")
                if app != "catalog" and payload.get("application") != APPLICATIONS[app]:
                    raise ReleaseError("wrong application")
                if payload.get("worker_pid") not in wanted:
                    raise ReleaseError("health is not served by current target worker")
                seen[public].add(payload["worker_pid"])
            consecutive += 1
            if consecutive >= 3 and all(wanted <= samples for samples in seen.values()):
                after = assert_protected(protected, app)
                return {"workers": sorted(wanted), "identity": live, "protected_after": after}
        except (OSError, ValueError, ReleaseError) as exc:
            last_error = str(exc)
            consecutive = 0
        # Bounded, paced sampling; no 2048-request burst during worker startup.
        time.sleep(1)
    raise ReleaseError("target verification timed out: " + last_error)


def stage(app, sha, generation, cfg):
    release = ROOT / app / "releases" / sha
    if release.exists():
        raise ReleaseError("candidate directory already exists; preserve for inspection")
    app_dir = release / "app"
    app_dir.mkdir(parents=True)
    safe_extract(git("archive", sha, *PACKAGES[app], binary=True), app_dir)
    atomic_write(app_dir / ".release-commit", sha + "\n")
    atomic_write(app_dir / ".release-generation", str(generation) + "\n")
    python = release / "venv/bin/python"
    run(sys.executable, "-m", "venv", release / "venv")
    # Locked existing production dependencies, installed into a private venv.
    # Never pip-install into /opt/rachelcode/venv.
    run(python, "-m", "pip", "install", "--disable-pip-version-check", "-r",
        "/etc/rachelcode/%s-requirements.lock" % app, timeout=300)
    run(python, "-m", "pip", "check")
    run(python, "-m", "compileall", "-q", app_dir)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CATALOG_", "PLANNING_", "REPLENISH_", "PYTHON"))}
    module = APPLICATIONS[app] + ".release"
    prefix = {"planning": "PLANNING", "catalog": "CATALOG", "replenishment": "REPLENISHMENT"}[app]
    expression = ("import json; from %s import %s_SOURCE_FINGERPRINT as f; "
                  "print(json.dumps({'source_fingerprint':f}))" % (module, prefix))
    expected = json.loads(run(python, "-c", expression, cwd=app_dir, env=env))
    # Exercise initialization ONLY in a temporary database. No live DB arguments.
    preflight = ("import tempfile; from pathlib import Path; from %s.db import init_db; "
                 "d=tempfile.TemporaryDirectory(); init_db(Path(d.name)/'preflight.db',seed_demo=False); "
                 "print('PREFLIGHT_OK')" % APPLICATIONS[app])
    run(python, "-c", preflight, cwd=app_dir, env=env)
    expected.update(release_commit=sha, release_generation=generation, workers=cfg.get("workers", 1))
    return app_dir, python, expected


def deploy(sha, config):
    valid_sha(sha)
    # Same lock used by the retired dispatcher: queued legacy jobs cannot race.
    with lock(AUTOMATION / "deploy.lock"):
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = str(AUTOMATION / "git-askpass.sh")
        run("git", "--git-dir", AUTOMATION / "repository.git", "fetch", "--quiet", "origin",
            "+refs/heads/main:refs/remotes/origin/main", env=env)
        if git("rev-parse", "refs/remotes/origin/main") != sha:
            raise ReleaseError("stale webhook: requested commit is not origin/main")
        plan = json.loads(git("show", sha + ":deploy/release-plan.json"))
        app = plan.get("application")
        if app not in APPS:
            raise ReleaseError("unknown target")
        cfg = config["apps"].get(app, {})
        state_path = ROOT / app / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "accepted_commit": cfg.get("baseline_commit"), "generation": cfg.get("baseline_generation")}
        if state["accepted_commit"] == sha:
            print("ALREADY_ACCEPTED " + app + " " + sha)
            return
        base = valid_sha(state["accepted_commit"])
        git("merge-base", "--is-ancestor", base, sha)
        paths = git("diff", "--name-only", "--no-renames", base, sha).splitlines()
        validate_plan(plan, git("show", "-s", "--format=%B", sha), paths, base,
                      [name for name, item in config["apps"].items() if item.get("enabled") is True])
        check_no_schema_change(app, base, sha)
        generation = int(git("rev-list", "--count", sha))
        if generation <= state["generation"]:
            raise ReleaseError("candidate is not newer than accepted target release")
        prior = health(app)
        if (prior.get("release_commit") != base or prior.get("status") != "ok"
                or not prior.get("db_exists")):
            raise ReleaseError("target runtime and accepted release disagree")
        protected = {name: identity(name) for name in APPS if name != app}
        identity(app)
        app_dir, python, expected = stage(app, sha, generation, cfg)
        assert_protected(protected, app)
        # Staging may take minutes; recheck source ordering just before activation.
        run("git", "--git-dir", AUTOMATION / "repository.git", "fetch", "--quiet", "origin",
            "+refs/heads/main:refs/remotes/origin/main", env=env)
        if git("rev-parse", "refs/remotes/origin/main") != sha:
            raise ReleaseError("newer origin push arrived during staging")
        dropin = SYSTEMD_DIR / (SERVICES[app] + ".service.d/90-isolated-release.conf")
        evidence = activation(app, dropin, dropin_text(app, app_dir, python, sha, generation,
                              expected["workers"]), lambda: verify_target(app, expected, protected))
        state.update(accepted_commit=sha, generation=generation, source_fingerprint=expected["source_fingerprint"],
                     runtime=str(app_dir), protected_before=protected, acceptance=evidence,
                     accepted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        atomic_write(state_path, json.dumps(state, indent=2) + "\n")
        pointer = ROOT / app / "current"
        temporary = pointer.with_name(".current-" + sha)
        temporary.symlink_to(app_dir)
        os.replace(temporary, pointer)
        print("ACCEPTED " + app + " " + sha + " " + expected["source_fingerprint"])


def attest(config):
    wrapper = AUTOMATION / "deploy.sh"
    expected = "#!/bin/sh\nset -eu\nexec /usr/bin/python3 /opt/rachelcode/automation/isolated-v1/isolated_release.py \"$@\"\n"
    if wrapper.read_text() != expected:
        raise ReleaseError("installed dispatcher wrapper is not isolated")
    timer = run("systemctl", "show", "rachel-catalog-post-release-verify.timer", "--property=ActiveState", "--value")
    if timer not in {"inactive", "failed"}:
        raise ReleaseError("legacy delayed verifier is still active")
    for app in APPS:
        dependencies = run("systemctl", "show", SERVICES[app],
                           "--property=PartOf,BindsTo,PropagatesStopTo", "--no-pager")
        if any(SERVICES[other] in dependencies for other in APPS if other != app):
            raise ReleaseError("cross-application systemd stop/restart dependency: " + app)
    hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
              for name in ("isolated_release.py", "release_scope.py")}
    return {"version": VERSION, "installed_hashes": hashes,
            "enabled_apps": [name for name, cfg in config["apps"].items() if cfg.get("enabled") is True],
            "services": {app: identity(app) for app in APPS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", nargs="?")
    parser.add_argument("--attest", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(CONFIG.read_text())
        if config.get("version") != VERSION:
            raise ReleaseError("isolation has not been installed")
        if args.attest:
            print(json.dumps(attest(config), sort_keys=True))
        else:
            if not args.commit:
                raise ReleaseError("exact commit required")
            attest(config)
            deploy(args.commit, config)
    except (OSError, ValueError, subprocess.SubprocessError, ReleaseError) as exc:
        print("RELEASE_BLOCKED " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
