#!/usr/bin/env python3
"""One-time, root-only installation. Does not restart any business website.

Run from a transferred, reviewed scripts directory, NOT through a git hook.
Existing shared code, venv, business databases and uploads are never modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

from isolated_release import (APPS, AUTOMATION, CONFIG, ROOT, VERSION, ReleaseError,
                              atomic_write, attest, health, identity, lock, run)
from release_status import FILES, OriginStatus, READY, manifest, post_ready


WRAPPER = '#!/bin/sh\nset -eu\nexec /usr/bin/python3 /opt/rachelcode/automation/isolated-v1/isolated_release.py "$@"\n'

def status_preflight():
    """Verify origin status write permission before touching the dispatcher."""
    client = OriginStatus(credential_file=CONFIG.parent / "origin-status.json")
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(AUTOMATION / "git-askpass.sh"))
    origin = run("git", "--git-dir", AUTOMATION / "repository.git", "ls-remote", "origin",
                 "refs/heads/main", env=env).split()[0]
    client.post(origin, READY, "pending", "Installing reviewed automatic isolation receipts; business sites unchanged")
    return client, origin


def provision_status_credential():
    """One-time stdin-only setup for a dedicated origin write:repository token.

    Never use the root login password, overwrite the read-only Git credential,
    or print the received token. Application services cannot read this file.
    """
    if os.geteuid() != 0:
        raise ReleaseError("root required")
    path = CONFIG.parent / "origin-status.json"
    if path.exists():
        raise ReleaseError("status credential already exists; refusing overwrite")
    values = json.loads(sys.stdin.read(16384))
    if (set(values) != {"username", "password"} or any(
            not isinstance(value, str) or not value or "\n" in value for value in values.values())):
        raise ReleaseError("invalid dedicated status credential")
    atomic_write(path, json.dumps(values) + "\n", 0o600)
    print("Dedicated origin status credential installed; read-only Git credential unchanged")


def upgrade():
    """Replace reviewed control-plane code, leaving app/config/data intact."""
    if os.geteuid() != 0:
        raise ReleaseError("root required")
    config = json.loads(CONFIG.read_text())
    if config.get("version") != VERSION:
        raise ReleaseError("unexpected installed dispatcher version")
    before = {app: identity(app) for app in APPS}
    installed = AUTOMATION / VERSION
    source = Path(__file__).resolve().parent
    for name in FILES:
        if not (source / name).is_file():
            raise ReleaseError("reviewed dispatcher source incomplete")
    with lock(AUTOMATION / "deploy.lock"):
        client, origin = status_preflight()
        backup = AUTOMATION / "isolation-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-dispatcher")
        backup.mkdir(parents=True, mode=0o700)
        for name in FILES:
            if (installed / name).exists():
                shutil.copy2(installed / name, backup / name)
        shutil.copy2(CONFIG, backup / CONFIG.name)
        run("systemctl", "stop", "rachel-deploy-webhook")
        try:
            atomic_write(AUTOMATION / "deploy.sh", "#!/bin/sh\necho 'RELEASE_BLOCKED reviewed dispatcher upgrade incomplete' >&2\nexit 78\n", 0o755)
            for name in FILES:
                atomic_write(installed / name, (source / name).read_bytes())
            for app in APPS:
                if config["apps"][app].get("enabled"):
                    state = json.loads((ROOT / app / "state.json").read_text())
                    app_dir = ROOT / app / "releases" / state["accepted_commit"] / "app"
                    if before[app]["WorkingDirectory"] != str(app_dir):
                        raise ReleaseError("accepted service runtime differs from state")
                    # Upgrade the dispatcher only, not any app's runtime pointer.
                    if (ROOT / app / "current").resolve() != app_dir.resolve():
                        raise ReleaseError("accepted runtime pointer differs from state")
            config["installed_hashes"] = manifest(installed)
            atomic_write(CONFIG, json.dumps(config, indent=2) + "\n", 0o600)
            atomic_write(AUTOMATION / "deploy.sh", WRAPPER, 0o755)
            proof = attest(config)
            if proof["services"] != before:
                raise ReleaseError("business service identity changed during dispatcher upgrade")
            atomic_write(backup / "installed-proof.json", json.dumps(proof, indent=2), 0o600)
            post_ready(client, origin, proof)
            print(json.dumps({"upgraded": VERSION, "backup": str(backup), "proof": proof}, sort_keys=True))
        finally:
            run("systemctl", "start", "rachel-deploy-webhook")


def install():
    if os.geteuid() != 0:
        raise ReleaseError("root required to install reviewed dispatcher")
    if sys.version_info < (3, 9):
        raise ReleaseError("server dispatcher requires Python 3.9 or later")
    if CONFIG.exists():
        raise ReleaseError("isolation config already exists; review installed state instead of overwriting")
    # Source transfer paths must not create a runtime dependent on a temporary
    # checkout. Only the reviewed dispatcher files are installed below.
    before = {app: identity(app) for app in APPS}
    baseline = {app: health(app) for app in APPS}
    # No business requests are used here other than three small health reads.
    for app, state in before.items():
        if state["WorkingDirectory"] != "/opt/rachelcode/app":
            raise ReleaseError("unexpected existing runtime directory: " + app)
    with lock(AUTOMATION / "deploy.lock"):
        client, origin = status_preflight()
        pending = run("systemctl", "list-units", "--all", "--plain", "--no-legend", "rachel-auto-deploy-*.service")
        if any(" running " in line or " activating " in line for line in pending.splitlines()):
            raise ReleaseError("legacy deployment is still running")
        old_verify = run("systemctl", "show", "rachel-catalog-post-release-verify.service", "--property=ActiveState", "--value")
        if old_verify not in {"inactive", "failed"}:
            raise ReleaseError("legacy delayed verifier is still running")
        backup = AUTOMATION / "isolation-backups" / time.strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, mode=0o700)
        for name in ("deploy.sh", "verify_release.py", "webhook_server.py"):
            shutil.copy2(AUTOMATION / name, backup / name)
        atomic_write(backup / "baseline.json", json.dumps({"services": before, "health": baseline}, indent=2), 0o600)
        # Pause the deployment listener only, not any of the three websites.
        # This prevents an old shell job being read while replacing its entrypoint.
        run("systemctl", "stop", "rachel-deploy-webhook")
        try:
            # If installation stops halfway, future pushes remain blocked;
            # never leave the unsafe legacy dispatcher available for fallback.
            atomic_write(AUTOMATION / "deploy.sh", "#!/bin/sh\necho 'RELEASE_BLOCKED isolation installation incomplete' >&2\nexit 78\n", 0o755)
            run("systemctl", "disable", "--now", "rachel-catalog-post-release-verify.timer")
            installed = AUTOMATION / VERSION
            installed.mkdir(mode=0o755, exist_ok=False)
            source = Path(__file__).resolve().parent
            for name in FILES:
                atomic_write(installed / name, (source / name).read_bytes())
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            env_source = ROOT / "app/.env"
            env_lines = env_source.read_text().splitlines() if env_source.exists() else []
            # Planning gets only its own settings. No credential is printed.
            planning_lines = [line for line in env_lines if line.strip().startswith("PLANNING_")]
            env_path = CONFIG.parent / "planning.env"
            if env_path.exists():
                raise ReleaseError("planning config already exists; refusing overwrite")
            atomic_write(env_path, "\n".join(planning_lines) + "\n", 0o600)
            # systemd (root) reads EnvironmentFile before changing service user.
            frozen = run(ROOT / "venv/bin/python", "-c",
                         "import importlib.metadata as m; print('\\n'.join(sorted({d.metadata['Name']+'=='+d.version for d in m.distributions() if d.metadata['Name'].lower() not in {'pip','setuptools','wheel'}})))")
            atomic_write(CONFIG.parent / "planning-requirements.lock", frozen + "\n")
            config = {"version": VERSION, "apps": {app: {
                "enabled": app == "planning", "baseline_commit": baseline[app]["release_commit"],
                "baseline_generation": baseline[app]["release_generation"],
                "workers": 8 if app == "catalog" else 1} for app in APPS}}
            config["installed_hashes"] = manifest(installed)
            (ROOT / "planning/releases").mkdir(parents=True, exist_ok=True)
            atomic_write(CONFIG, json.dumps(config, indent=2) + "\n", 0o600)
            atomic_write(AUTOMATION / "deploy.sh", WRAPPER, 0o755)
            proof = attest(config)
            if proof["services"] != before:
                raise ReleaseError("business service identity changed during installation")
            atomic_write(backup / "installed-proof.json", json.dumps(proof, indent=2), 0o600)
            post_ready(client, origin, proof)
            print(json.dumps({"installed": VERSION, "backup": str(backup), "proof": proof}, sort_keys=True))
        finally:
            # Never reinstate the unsafe all-site dispatcher on an install error.
            run("systemctl", "start", "rachel-deploy-webhook")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="Upgrade reviewed dispatcher only; no business service restart")
    parser.add_argument("--provision-status-credential", action="store_true", help="Read a dedicated origin token from stdin; no site changes")
    args = parser.parse_args()
    if args.provision_status_credential:
        provision_status_credential()
    else:
        upgrade() if args.upgrade else install()
