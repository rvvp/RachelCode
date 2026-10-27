from __future__ import annotations

import ast
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import isolated_release as release
import migrate_isolated_app as migration


OLD = "a" * 40
PARENT = "b" * 40
NEW = "c" * 40


class IsolatedMigrationTests(unittest.TestCase):
    def test_each_app_gets_only_its_own_effective_environment(self):
        source = {"CATALOG_PLANNING_API_TOKEN": "catalog-secret", "PLANNING_CATALOG_API_TOKEN": "planning-secret",
                  "PLANNING_BOOTSTRAP_ADMIN_PASSWORD": "never-copy", "REPLENISH_CREDENTIAL_KEY": "key",
                  "TMALL_APP_SECRET": "tmall-secret", "VIPSHOP_ACCESS_TOKEN": "vip-secret"}
        catalog = migration.selected_environment("catalog", source, {"CATALOG_IMPORT_SLOTS": "2"})
        replenishment = migration.selected_environment("replenishment", source, {})
        self.assertEqual(catalog["CATALOG_PLANNING_API_TOKEN"], "catalog-secret")
        self.assertEqual(catalog["CATALOG_IMPORT_SLOTS"], "2")
        self.assertTrue(all(key.startswith("CATALOG_") for key in catalog))
        self.assertEqual(replenishment["REPLENISH_CREDENTIAL_KEY"], "key")
        self.assertEqual(replenishment["TMALL_APP_SECRET"], "tmall-secret")
        self.assertNotIn("PLANNING_BOOTSTRAP_ADMIN_PASSWORD", catalog | replenishment)

    def test_runtime_environment_cannot_redirect_live_data_or_seed_examples(self):
        for app, key in [("catalog", "CATALOG_DB"), ("catalog", "CATALOG_UPLOADS"),
                          ("replenishment", "REPLENISH_DB")]:
            with self.subTest(app=app, key=key), self.assertRaises(release.ReleaseError):
                migration.selected_environment(app, {key: "/wrong/path"}, {})
        values = migration.selected_environment("catalog", {"CATALOG_SEED_DEMO": "1"}, {})
        self.assertEqual(values["CATALOG_SEED_DEMO"], "0")
        self.assertEqual(values["CATALOG_SEED_SAMPLES"], "0")

    def test_existing_service_values_win_over_legacy_file(self):
        values = migration.selected_environment("catalog", {"CATALOG_BRAND_MARK": "old"},
                                                {"CATALOG_BRAND_MARK": "current", "CATALOG_RELEASE_COMMIT": OLD})
        self.assertEqual(values["CATALOG_BRAND_MARK"], "current")
        self.assertNotIn("CATALOG_RELEASE_COMMIT", values)

    def test_online_backup_contains_uncheckpointed_wal_and_never_replaces_live_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "catalog/catalog.db"
            live.parent.mkdir()
            backup = root / "backup"
            backup.mkdir()
            connection = sqlite3.connect(live)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO products VALUES(1, 'new live data')")
            connection.commit()
            inode = live.stat().st_ino
            with patch.object(migration, "DATA_ROOT", root):
                actual, snapshot = migration.backup_database("catalog", backup)
            self.assertEqual(actual, live)
            self.assertEqual(live.stat().st_ino, inode)
            with sqlite3.connect(snapshot) as copy:
                self.assertEqual(copy.execute("SELECT * FROM products").fetchall(), [(1, "new live data")])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
            connection.close()

    def test_rehearsal_detects_startup_mutation_without_touching_live_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "catalog.db"
            with sqlite3.connect(snapshot) as connection:
                connection.execute("CREATE TABLE products(id INTEGER PRIMARY KEY, price INTEGER)")
                connection.execute("INSERT INTO products VALUES(1, 599)")
            original = migration.table_fingerprints(snapshot)
            def mutate(*args, **kwargs):
                with sqlite3.connect(root / "initialization-rehearsal.db") as connection:
                    connection.execute("UPDATE products SET price=1")
            with patch.object(release, "run", side_effect=mutate):
                with self.assertRaisesRegex(release.ReleaseError, "startup changes business tables"):
                    migration.rehearse("catalog", root, root / "python", {}, snapshot, root)
            self.assertEqual(migration.table_fingerprints(snapshot), original)

    def test_only_known_layout_refresh_time_is_ignored_never_setting_values(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "catalog.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE app_settings(setting_key TEXT, setting_value TEXT, updated_at TEXT)")
                connection.execute("INSERT INTO app_settings VALUES('list_layout_fields_B', 'same', 'old')")
            before = migration.table_fingerprints(db)
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE app_settings SET updated_at='new'")
            self.assertEqual(migration.table_fingerprints(db), before)
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE app_settings SET setting_value='changed'")
            self.assertNotEqual(migration.table_fingerprints(db), before)

    def test_runtime_enrollment_rejects_business_or_schema_changes(self):
        for path in ("catalog_backend/db.py", "catalog_backend/web.py", "app.py", "requirements.txt"):
            with patch.object(release, "git", return_value=path), self.subTest(path=path):
                with self.assertRaisesRegex(release.ReleaseError, "business code"):
                    migration.ensure_runtime_only_change("catalog", OLD, NEW)
        with patch.object(release, "git", return_value="catalog_backend/release.py\nscripts/isolated_release.py"):
            migration.ensure_runtime_only_change("catalog", OLD, NEW)

    def test_private_cache_requires_exact_local_wheel_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "planning/wheelhouse"
            source.mkdir(parents=True)
            (source / "package.whl").write_bytes(b"tampered")
            config = root / "etc"
            config.mkdir()
            (config / "planning-requirements.lock").write_text("--no-index\n--require-hashes\n--find-links=" + str(source) + "\npkg==1 --hash=sha256:bad\n")
            with patch.object(release, "ROOT", root), patch.object(release, "CONFIG", config / "isolation.json"):
                with self.assertRaisesRegex(release.ReleaseError, "verified lock"):
                    migration.prepare_dependencies("catalog", root)
            self.assertFalse((config / "catalog-requirements.lock").exists())

    def test_other_app_history_is_excluded_but_new_out_of_scope_changes_still_fail(self):
        metadata = {"schema": 1, "application": "catalog", "base_commit": OLD,
                    "repository_base_commit": PARENT, "reviewed_paths": [], "database_migration": False}
        def git(*args):
            if args[0] == "rev-parse": return PARENT
            if args[0] == "diff":
                return "catalog_backend/web.py" if "--" in args else "catalog_backend/web.py\nAPPLICATION_BOUNDARIES.md"
            return ""
        paths = release.review_paths(metadata, OLD, NEW, git)
        release.validate_plan(metadata, "Release-App: catalog", paths, OLD, ["catalog"])
        with self.assertRaisesRegex(release.ReleaseError, "another application"):
            release.validate_plan(metadata, "Release-App: catalog", paths + ["planning_center/web.py"], OLD, ["catalog"])

    def test_old_dependency_changes_are_not_hidden_by_repository_base(self):
        metadata = {"application": "replenishment", "base_commit": OLD, "repository_base_commit": PARENT,
                    "reviewed_paths": []}
        def git(*args):
            if args[0] == "rev-parse": return PARENT
            if args[0] == "diff": return "catalog_backend/release.py" if "--" in args else "README.md"
            return ""
        with self.assertRaisesRegex(release.ReleaseError, "dependency needs explicit review"):
            release.review_paths(metadata, OLD, NEW, git)
        metadata["reviewed_paths"] = ["catalog_backend/release.py"]
        self.assertEqual(release.review_paths(metadata, OLD, NEW, git), ["README.md"])

    def test_repository_base_cannot_skip_unreviewed_commits(self):
        metadata = {"application": "catalog", "repository_base_commit": OLD}
        with self.assertRaisesRegex(release.ReleaseError, "immediate parent"):
            release.review_paths(metadata, OLD, NEW, lambda *args: PARENT)

    def test_every_app_has_a_separate_write_boundary_and_graceful_stop(self):
        for app in release.APPS:
            directory = Path("/opt/rachelcode") / app / "releases" / NEW / "app"
            config = release.dropin_text(app, directory, directory.parent / "venv/bin/python", NEW, 147)
            self.assertIn("ProtectSystem=strict", config)
            self.assertIn("ReadWritePaths=\nReadWritePaths=/data/rachelcode/" + app, config)
            self.assertIn("TimeoutStopSec=130", config)
            for other in set(release.APPS) - {app}:
                self.assertNotIn("/data/rachelcode/" + other, config)

    def test_install_templates_cannot_reintroduce_legacy_shared_runtime(self):
        for app in release.APPS:
            content = (ROOT / "deploy/systemd" / ("rachel-" + app + ".service")).read_text()
            self.assertIn("WorkingDirectory=/opt/rachelcode/" + app + "/current", content)
            self.assertIn("EnvironmentFile=/etc/rachelcode/" + app + ".env", content)
            self.assertIn("ReadWritePaths=/data/rachelcode/" + app + "\n", content)
            self.assertNotIn("/opt/rachelcode/venv", content)
            self.assertNotIn("/opt/rachelcode/app", content)
            self.assertNotIn("PartOf=", content)
            self.assertNotIn("BindsTo=", content)

    def test_runtime_venv_alias_keeps_python_in_its_private_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            app_dir = root / "catalog/releases" / NEW / "app"
            app_dir.mkdir(parents=True)
            private = app_dir.parent / "venv"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(private)], check=True, capture_output=True)
            with patch.object(release, "ROOT", root):
                release.runtime_pointers("catalog", app_dir)
            alias = root / "catalog/venv/bin/python"
            prefix = subprocess.check_output([str(alias), "-c", "import sys; print(sys.prefix)"], text=True).strip()
            self.assertEqual(Path(prefix).resolve(), private.resolve())
            self.assertEqual((root / "catalog/current").resolve(), app_dir)

    def test_migration_tools_remain_python39_compatible_and_do_not_restore(self):
        for name in ("migrate_isolated_app.py", "install_isolated_dispatcher.py"):
            source = (ROOT / "scripts" / name).read_text()
            ast.parse(source, feature_version=(3, 9))
            self.assertNotIn("restore.sh", source)
            self.assertNotIn("VACUUM", source)

    def exercise_activation(self, root, app, fail=False):
        automation = root / "automation"
        backup = automation / "isolation-backups/test"
        backup.mkdir(parents=True)
        etc = root / "etc"
        etc.mkdir()
        config = {"version": release.VERSION, "apps": {name: {"enabled": name == "planning"} for name in release.APPS}}
        config_path = etc / "isolation.json"
        config_path.write_text(json.dumps(config))
        env_path = etc / (app + ".env")
        env_path.write_text("PRIVATE_SETTING=unchanged\n")
        db = root / (app + ".db")
        db.write_bytes(b"current business data")
        app_dir = root / app / "releases" / NEW / "app"
        app_dir.mkdir(parents=True)
        identities = {name: {"MainPID": str(i + 10)} for i, name in enumerate(release.APPS)}
        prior = {"release_commit": OLD, "release_generation": 8, "source_fingerprint": "old"}
        expected = {"release_commit": NEW, "release_generation": 9, "source_fingerprint": "new", "workers": 8 if app == "catalog" else 1}
        proof = {"application": app, "commit": NEW, "app_dir": str(app_dir),
                 "python": str(app_dir.parent / "venv/bin/python"), "services_before": identities,
                 "prior_health": prior, "database": str(db), "database_inode": db.stat().st_ino,
                 "config_sha256": hashlib.sha256(env_path.read_bytes()).hexdigest(), "expected": expected}
        path = backup / "prepared.json"
        path.write_text(json.dumps(proof))
        with ExitStack() as stack:
            stack.enter_context(patch.object(migration.os, "geteuid", return_value=0))
            for key, value in {"ROOT": root, "AUTOMATION": automation, "CONFIG": config_path,
                               "SYSTEMD_DIR": etc}.items():
                stack.enter_context(patch.object(release, key, value))
            stack.enter_context(patch.object(release, "attest", return_value={}))
            stack.enter_context(patch.object(release, "identity", side_effect=lambda name: identities[name]))
            stack.enter_context(patch.object(release, "git", return_value=NEW))
            stack.enter_context(patch.object(release, "health", return_value=prior))
            commands = stack.enter_context(patch.object(release, "run", return_value=""))
            verifier = stack.enter_context(patch.object(release, "verify_target", return_value={"workers": [17]}))
            if fail:
                verifier.side_effect = release.ReleaseError("target not ready")
            error = None
            try:
                migration.activate(app, path)
            except release.ReleaseError as exc:
                error = exc
        self.assertEqual(db.read_bytes(), b"current business data")
        return error, commands, json.loads(config_path.read_text())

    def test_each_migration_enables_only_the_accepted_target(self):
        for app in migration.MIGRATION_APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                error, commands, config = self.exercise_activation(root, app)
                self.assertIsNone(error)
                self.assertTrue(config["apps"][app]["enabled"])
                other = next(name for name in migration.MIGRATION_APPS if name != app)
                self.assertFalse(config["apps"][other]["enabled"])
                self.assertFalse((root / other / "state.json").exists())
                self.assertEqual([call.args for call in commands.call_args_list if "restart" in call.args],
                                 [("systemctl", "restart", "rachel-" + app)])
                self.assertEqual((root / app / "current").resolve(), (root / app / "releases" / NEW / "app").resolve())

    def test_failed_migration_rolls_back_only_target_code_and_keeps_enrollment_disabled(self):
        for app in migration.MIGRATION_APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                error, commands, config = self.exercise_activation(root, app, fail=True)
                self.assertIn("not ready", str(error))
                self.assertFalse(config["apps"][app]["enabled"])
                self.assertFalse((root / app / "state.json").exists())
                self.assertFalse((root / app / "current").exists())
                self.assertEqual([call.args for call in commands.call_args_list if "restart" in call.args],
                                 [("systemctl", "restart", "rachel-" + app)] * 2)


if __name__ == "__main__":
    unittest.main()
