#!/usr/bin/env python3
"""Explicit, one-app enrollment into the already installed isolated dispatcher.

Infrastructure operation, not a Webhook handler. Source must already exist on
origin/main. Never copies/restores live databases or uploads into a release.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import time

import isolated_release as release


MIGRATION_APPS = ("catalog", "replenishment")
DATA_ROOT = Path("/data/rachelcode")
ENV_PREFIXES = {"catalog": ("CATALOG_",),
                "replenishment": ("REPLENISH_", "TMALL_", "VIPSHOP_")}


def selected_environment(app, file_values, process_values):
    """Preserve effective values without handing another site's secrets to it."""
    values = {key: value for source in (file_values, process_values)
              for key, value in source.items() if key.startswith(ENV_PREFIXES[app])}
    for key in ("CATALOG_RELEASE_COMMIT", "CATALOG_RELEASE_GENERATION"):
        values.pop(key, None)
    prefix = "CATALOG" if app == "catalog" else "REPLENISH"
    expected_db = "/data/rachelcode/%s/%s.db" % (app, app)
    if values.get(prefix + "_DB", expected_db) != expected_db:
        raise release.ReleaseError("unexpected existing database path")
    values.update({prefix + "_DB": expected_db, prefix + "_RUNTIME_MODE": "production",
                   prefix + "_SEED_DEMO": "0"})
    if app == "catalog":
        expected_uploads = "/data/rachelcode/catalog/uploads"
        if values.get("CATALOG_UPLOADS", expected_uploads) != expected_uploads:
            raise release.ReleaseError("unexpected existing uploads path")
        values.update(CATALOG_UPLOADS=expected_uploads, CATALOG_SEED_SAMPLES="0")
    return values


def env_text(values):
    # EnvironmentFile uses shell-like quoting, but never executes substitutions.
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'
    if any("\n" in key or "=" in key or "\x00" in value for key, value in values.items()):
        raise release.ReleaseError("invalid environment entry")
    return "\n".join(key + "=" + quote(value) for key, value in sorted(values.items())) + "\n"


def parse_env(path):
    result = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip("'\"")
    return result


def table_fingerprints(path):
    result = {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15) as conn:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = conn.execute("SELECT * FROM " + quoted)
            columns = [field[0] for field in rows.description]
            hashes = []
            for row in rows:
                # Existing Catalog startup re-saves the same B-list layout.
                # Ignore ONLY its refresh time; keep the key and actual value.
                if name == "app_settings" and row[columns.index("setting_key")] == "list_layout_fields_B":
                    row = tuple(None if key == "updated_at" else value for key, value in zip(columns, row))
                hashes.append(hashlib.sha256(repr(row).encode()).digest())
            result[name] = {"count": len(hashes), "sha256": hashlib.sha256(b"".join(sorted(hashes))).hexdigest()}
    return result


def backup_database(app, backup):
    live = DATA_ROOT / app / (app + ".db")
    snapshot = backup / (app + ".db")
    with sqlite3.connect(live.as_uri() + "?mode=ro", uri=True, timeout=15) as src:
        with sqlite3.connect(snapshot) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise release.ReleaseError("database backup quick_check failed")
    os.chmod(snapshot, 0o600)
    # Keep the original encryption key at the original path; a private backup
    # protects recovery without creating a new production identity.
    key = live.with_suffix(".key")
    if app == "replenishment" and key.exists():
        shutil.copy2(key, backup / key.name)
        os.chmod(backup / key.name, 0o600)
    return live, snapshot


def rehearse(app, app_dir, python, values, snapshot, backup):
    copy = backup / "initialization-rehearsal.db"
    shutil.copy2(snapshot, copy)
    before = table_fingerprints(copy)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CATALOG_", "PLANNING_", "REPLENISH_", "TMALL_", "VIPSHOP_", "PYTHON"))}
    env.update(values)
    prefix = "CATALOG" if app == "catalog" else "REPLENISH"
    env[prefix + "_DB"] = str(copy)
    expression = ("import sys; from %s.db import init_db; "
                  "init_db(sys.argv[1], seed_demo=False); print('INITIALIZATION_OK')" % release.APPLICATIONS[app])
    release.run(python, "-c", expression, copy, cwd=app_dir, env=env)
    after = table_fingerprints(copy)
    if before != after:
        changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        raise release.ReleaseError("startup changes business tables in rehearsal: " + ", ".join(changed))
    return before


def ensure_runtime_only_change(app, base, sha):
    paths = release.git("diff", "--name-only", "--no-renames", base, sha, "--", *release.PACKAGES[app]).splitlines()
    # Preserve all domain code and schemas during infrastructure enrollment.
    allowed = {"catalog_backend/release.py"}
    unexpected = [p for p in paths if p not in allowed and not p.startswith(("scripts/", "deploy/"))]
    if unexpected:
        raise release.ReleaseError("migration would change business code: " + ", ".join(unexpected))


def prepare_dependencies(app, backup):
    # Copy verified local wheels into a target-owned cache. No network required.
    source = release.ROOT / "planning/wheelhouse"
    target = release.ROOT / app / "wheelhouse"
    lock_source = release.CONFIG.parent / "planning-requirements.lock"
    content = lock_source.read_text()
    if not all(flag in content for flag in ("--no-index", "--require-hashes", str(source))):
        raise release.ReleaseError("verified offline dependency lock is missing")
    if target.exists():
        raise release.ReleaseError("target wheelhouse already exists; inspect previous attempt")
    target.mkdir(parents=True)
    hashes = {}
    for wheel in sorted(source.glob("*.whl")):
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest not in content:
            raise release.ReleaseError("wheel is not present in verified lock")
        shutil.copy2(wheel, target / wheel.name)
        hashes[wheel.name] = digest
    if not hashes:
        raise release.ReleaseError("no verified dependency wheels")
    lock = release.CONFIG.parent / (app + "-requirements.lock")
    if lock.exists():
        raise release.ReleaseError("target dependency lock already exists")
    release.atomic_write(lock, content.replace(str(source), str(target)))
    release.atomic_write(backup / "dependency-hashes.json", json.dumps(hashes, indent=2), 0o600)


def prepare(app, sha):
    release.valid_sha(sha)
    if os.geteuid() != 0:
        raise release.ReleaseError("root required")
    config = json.loads(release.CONFIG.read_text())
    release.attest(config)
    cfg = config["apps"][app]
    if cfg.get("enabled") or (release.ROOT / app / "state.json").exists():
        raise release.ReleaseError("target already enrolled")
    with release.lock(release.AUTOMATION / "deploy.lock"):
        if release.git("rev-parse", "refs/remotes/origin/main") != sha:
            raise release.ReleaseError("migration source must be the existing origin/main")
        before = {name: release.identity(name) for name in release.APPS}
        prior = release.health(app)
        base = release.valid_sha(prior.get("release_commit"))
        release.git("merge-base", "--is-ancestor", base, sha)
        ensure_runtime_only_change(app, base, sha)
        if before[app]["WorkingDirectory"] != "/opt/rachelcode/app":
            raise release.ReleaseError("unexpected legacy working directory")
        backup = release.AUTOMATION / "isolation-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + app)
        backup.mkdir(mode=0o700)
        live, snapshot = backup_database(app, backup)
        pid = before[app]["MainPID"]
        process_values = dict(item.split("=", 1) for item in Path("/proc/" + pid + "/environ").read_bytes().decode().split("\0") if "=" in item)
        file_values = parse_env(release.ROOT / "app/.env") if app == "catalog" else {}
        values = selected_environment(app, file_values, process_values)
        env_path = release.CONFIG.parent / (app + ".env")
        if env_path.exists():
            raise release.ReleaseError("target environment already exists; refusing overwrite")
        release.atomic_write(env_path, env_text(values), 0o600)
        prepare_dependencies(app, backup)
        generation = int(release.git("rev-list", "--count", sha))
        app_dir, python, expected = release.stage(app, sha, generation, cfg)
        tables = rehearse(app, app_dir, python, values, snapshot, backup)
        if before != {name: release.identity(name) for name in release.APPS}:
            raise release.ReleaseError("a service changed during preparation")
        proof = {"application": app, "commit": sha, "prior_health": prior, "services_before": before,
                 "backup": str(backup), "database": str(live), "database_inode": live.stat().st_ino,
                 "app_dir": str(app_dir), "python": str(python), "expected": expected,
                 "environment_names": sorted(values), "rehearsal_tables": tables,
                 "config_sha256": hashlib.sha256(env_path.read_bytes()).hexdigest()}
        release.atomic_write(backup / "prepared.json", json.dumps(proof, indent=2), 0o600)
        print(json.dumps({"prepared": app, "proof": str(backup / "prepared.json"), "business_tables_unchanged": len(tables)}))


def resume_preparation(app, sha, backup_path):
    """Recheck a preserved candidate after a rehearsal-only interruption."""
    release.valid_sha(sha)
    if os.geteuid() != 0:
        raise release.ReleaseError("root required")
    backup = Path(backup_path).resolve()
    if backup.parent != (release.AUTOMATION / "isolation-backups").resolve() or (backup / "prepared.json").exists():
        raise release.ReleaseError("resume requires an unaccepted preparation backup")
    config = json.loads(release.CONFIG.read_text())
    release.attest(config)
    if config["apps"][app].get("enabled"):
        raise release.ReleaseError("target already enabled")
    with release.lock(release.AUTOMATION / "deploy.lock"):
        if release.git("rev-parse", "refs/remotes/origin/main") != sha:
            raise release.ReleaseError("origin changed")
        before = {name: release.identity(name) for name in release.APPS}
        prior = release.health(app)
        if before[app]["WorkingDirectory"] != "/opt/rachelcode/app":
            raise release.ReleaseError("target is no longer on its legacy runtime")
        ensure_runtime_only_change(app, release.valid_sha(prior["release_commit"]), sha)
        pid = before[app]["MainPID"]
        process = dict(item.split("=", 1) for item in Path("/proc/" + pid + "/environ").read_bytes().decode().split("\0") if "=" in item)
        values = selected_environment(app, parse_env(release.ROOT / "app/.env") if app == "catalog" else {}, process)
        env_path = release.CONFIG.parent / (app + ".env")
        if env_path.read_text() != env_text(values):
            raise release.ReleaseError("prepared environment does not match existing service")
        app_dir = release.ROOT / app / "releases" / sha / "app"
        with tarfile.open(fileobj=io.BytesIO(release.git("archive", sha, *release.PACKAGES[app], binary=True))) as archive:
            for member in archive:
                if member.isfile() and (app_dir / member.name).read_bytes() != archive.extractfile(member).read():
                    raise release.ReleaseError("preserved source differs from origin")
        python = app_dir.parent / "venv/bin/python"
        release.run(python, "-m", "pip", "check")
        prefix = "CATALOG" if app == "catalog" else "REPLENISHMENT"
        expression = "from %s.release import %s_SOURCE_FINGERPRINT; print(%s_SOURCE_FINGERPRINT)" % (release.APPLICATIONS[app], prefix, prefix)
        fingerprint = release.run(python, "-c", expression, cwd=app_dir)
        live = DATA_ROOT / app / (app + ".db")
        # Create a fresh consistent snapshot in this preserved backup directory.
        fresh = backup / ("recheck-" + time.strftime("%H%M%S"))
        fresh.mkdir(mode=0o700)
        live, snapshot = backup_database(app, fresh)
        tables = rehearse(app, app_dir, python, values, snapshot, fresh)
        if before != {name: release.identity(name) for name in release.APPS}:
            raise release.ReleaseError("service changed during recheck")
        expected = {"release_commit": sha, "release_generation": int(release.git("rev-list", "--count", sha)),
                    "source_fingerprint": fingerprint, "workers": config["apps"][app].get("workers", 1)}
        proof = {"application": app, "commit": sha, "prior_health": prior, "services_before": before,
                 "backup": str(backup), "database": str(live), "database_inode": live.stat().st_ino,
                 "app_dir": str(app_dir), "python": str(python), "expected": expected,
                 "environment_names": sorted(values), "rehearsal_tables": tables,
                 "config_sha256": hashlib.sha256(env_path.read_bytes()).hexdigest()}
        release.atomic_write(backup / "prepared.json", json.dumps(proof, indent=2), 0o600)
        print(json.dumps({"prepared": app, "proof": str(backup / "prepared.json"), "business_tables_unchanged": len(tables)}))


def activate(app, proof_path):
    if os.geteuid() != 0:
        raise release.ReleaseError("root required")
    proof_path = Path(proof_path).resolve()
    if proof_path.parent.parent != (release.AUTOMATION / "isolation-backups").resolve():
        raise release.ReleaseError("proof must be inside the reviewed migration backup directory")
    proof = json.loads(proof_path.read_text())
    if proof["application"] != app:
        raise release.ReleaseError("proof target mismatch")
    sha = release.valid_sha(proof["commit"])
    expected_dir = release.ROOT / app / "releases" / sha / "app"
    if proof["app_dir"] != str(expected_dir) or proof["python"] != str(expected_dir.parent / "venv/bin/python"):
        raise release.ReleaseError("candidate is outside the target's private runtime")
    config = json.loads(release.CONFIG.read_text())
    release.attest(config)
    cfg = config["apps"][app]
    if cfg.get("enabled") or (release.ROOT / app / "state.json").exists():
        raise release.ReleaseError("target already enrolled")
    with release.lock(release.AUTOMATION / "deploy.lock"):
        before = {name: release.identity(name) for name in release.APPS}
        if before != proof["services_before"]:
            raise release.ReleaseError("service identities changed since preparation")
        if release.git("rev-parse", "refs/remotes/origin/main") != proof["commit"]:
            raise release.ReleaseError("origin advanced after preparation")
        prior = release.health(app)
        for key in ("release_commit", "release_generation", "source_fingerprint"):
            if prior.get(key) != proof["prior_health"].get(key):
                raise release.ReleaseError("target version changed after preparation")
        env_path = release.CONFIG.parent / (app + ".env")
        if hashlib.sha256(env_path.read_bytes()).hexdigest() != proof["config_sha256"]:
            raise release.ReleaseError("prepared environment changed")
        if Path(proof["database"]).stat().st_ino != proof["database_inode"]:
            raise release.ReleaseError("production database identity changed")
        pointer = release.ROOT / app / "current"
        if pointer.exists() or pointer.is_symlink():
            raise release.ReleaseError("unexpected existing runtime pointer")
        protected = {name: state for name, state in before.items() if name != app}
        dropin = release.SYSTEMD_DIR / (release.SERVICES[app] + ".service.d/90-isolated-release.conf")
        old_dropin = dropin.read_bytes() if dropin.exists() else None
        backup = proof_path.parent
        release.atomic_write(backup / "prior-dropin.json", json.dumps({"content": old_dropin.decode() if old_dropin else None}), 0o600)
        expected = proof["expected"]
        evidence = release.activation(app, dropin, release.dropin_text(app, Path(proof["app_dir"]),
                     Path(proof["python"]), proof["commit"], expected["release_generation"], expected["workers"]),
                     lambda: release.verify_target(app, expected, protected))
        state = {"accepted_commit": proof["commit"], "generation": expected["release_generation"],
                 "source_fingerprint": expected["source_fingerprint"], "runtime": proof["app_dir"],
                 "protected_before": protected, "acceptance": evidence, "migration_proof": str(proof_path),
                 "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        release.atomic_write(release.ROOT / app / "state.json", json.dumps(state, indent=2) + "\n")
        release.runtime_pointers(app, Path(proof["app_dir"]))
        cfg.update(enabled=True, baseline_commit=proof["commit"], baseline_generation=expected["release_generation"])
        release.atomic_write(release.CONFIG, json.dumps(config, indent=2) + "\n", 0o600)
        release.atomic_write(backup / "accepted.json", json.dumps(state, indent=2), 0o600)
        print(json.dumps({"accepted": app, "commit": proof["commit"], "backup": str(backup), "workers": evidence["workers"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, choices=MIGRATION_APPS)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--commit", required=True)
    activation = commands.add_parser("activate")
    activation.add_argument("--proof", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--commit", required=True)
    resume.add_argument("--backup", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.app, args.commit)
        elif args.command == "resume":
            resume_preparation(args.app, args.commit, args.backup)
        else:
            activate(args.app, args.proof)
    except (OSError, ValueError, sqlite3.Error, release.ReleaseError) as exc:
        print("MIGRATION_BLOCKED " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
