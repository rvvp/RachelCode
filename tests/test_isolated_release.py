from __future__ import annotations

import ast
from contextlib import ExitStack
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import isolated_release as release
import publish_isolated_release as publisher


OLD = "a" * 40
NEW = "b" * 40


def plan(app="planning", **extra):
    return dict(schema=1, application=app, base_commit=OLD,
                reviewed_paths=[], database_migration=False, **extra)


class IsolatedReleaseTests(unittest.TestCase):
    def test_explicit_app_and_matching_commit_authorization_required(self):
        for metadata, message, enabled in [({}, "", ["planning"]),
                (plan(), "ordinary commit", ["planning"]),
                (plan(), "Release-App: catalog", ["planning"]),
                (plan(), "Release-App: planning\nRelease-App: catalog", ["planning"]),
                (plan(), "Release-App: planning", [])]:
            with self.subTest(metadata=metadata, message=message):
                with self.assertRaises(release.ReleaseError):
                    release.validate_plan(metadata, message, [], OLD, enabled)

    def test_app_scope_never_expands_implicitly(self):
        for app, path in [("planning", "planning_center/web.py"),
                          ("catalog", "catalog_backend/web.py"),
                          ("replenishment", "replenishment_center/web.py")]:
            self.assertEqual(release.validate_plan(plan(app), "Release-App: " + app,
                             [path], OLD, [app]), app)
        for path in ["catalog_backend/web.py", "replenishment_center/db.py"]:
            metadata = plan()
            metadata["reviewed_paths"] = [path]
            with self.assertRaisesRegex(release.ReleaseError, "another application"):
                release.validate_plan(metadata, "Release-App: planning", [path], OLD, ["planning"])

    def test_runtime_data_remains_forbidden_even_when_listed_as_reviewed(self):
        for path in ["planning_data/planning.db", ".env", "backups/catalog.db", "data/uploads/a.jpg", "credentials.key"]:
            metadata = plan()
            metadata["reviewed_paths"] = [path]
            with self.assertRaises(release.ReleaseError):
                release.validate_plan(metadata, "Release-App: planning", [path], OLD, ["planning"])

    def test_shared_dependencies_require_review_without_releasing_provider(self):
        paths = ["catalog_backend/release.py", "scripts/isolated_release.py"]
        with self.assertRaises(release.ReleaseError):
            release.validate_plan(plan(), "Release-App: planning", paths, OLD, ["planning"])
        metadata = plan()
        metadata["reviewed_paths"] = paths
        self.assertEqual(release.validate_plan(metadata, "Release-App: planning", paths, OLD, ["planning"]), "planning")

    def test_stale_base_and_migration_cannot_use_code_only_release(self):
        metadata = plan()
        with self.assertRaisesRegex(release.ReleaseError, "currently accepted"):
            release.validate_plan(metadata, "Release-App: planning", [], NEW, ["planning"])
        metadata["database_migration"] = True
        with self.assertRaisesRegex(release.ReleaseError, "migrations"):
            release.validate_plan(metadata, "Release-App: planning", [], OLD, ["planning"])
        for path in ["planning_center/db.py", "planning_center/rotation.py", "requirements.txt"]:
            with patch.object(release, "git", return_value=path):
                with self.assertRaises(release.ReleaseError):
                    release.check_no_schema_change("planning", OLD, NEW)

    def test_archive_cannot_escape_into_other_apps_or_data(self):
        for name, kind in [("../../catalog/catalog.db", tarfile.REGTYPE),
                           ("/data/catalog.db", tarfile.REGTYPE),
                           ("data/planning.db", tarfile.REGTYPE),
                           ("source-link", tarfile.SYMTYPE),
                           ("source-link", tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                blob = io.BytesIO()
                with tarfile.open(fileobj=blob, mode="w") as archive:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.linkname = "/data/rachelcode/catalog/catalog.db"
                    archive.addfile(item)
                with self.assertRaises(release.ReleaseError):
                    release.safe_extract(blob.getvalue(), Path(temp))
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_private_runtime_and_exact_original_data_path_for_each_app(self):
        for app in release.APPS:
            app_dir = Path("/opt/rachelcode") / app / "releases" / NEW / "app"
            python = app_dir.parent / "venv/bin/python"
            text = release.dropin_text(app, app_dir, python, NEW, 10, 8 if app == "catalog" else 1)
            self.assertIn(str(app_dir), text)
            self.assertNotIn("/opt/rachelcode/venv", text)
            self.assertNotIn("restart", text)
            for other in set(release.APPS) - {app}:
                self.assertNotIn("/data/rachelcode/" + other, text)
            if app != "catalog":
                self.assertIn("/data/rachelcode/%s/%s.db" % (app, app), text)

    def test_failed_activation_rolls_back_target_code_only_and_preserves_data(self):
        for existing in (True, False):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                dropin = root / "planning.conf"
                if existing:
                    dropin.write_text("old planning code")
                db = root / "catalog.db"
                db.write_bytes(b"new business records must survive rollback")
                with patch.object(release, "run") as commands:
                    with self.assertRaisesRegex(release.ReleaseError, "unhealthy"):
                        release.activation("planning", dropin, "candidate planning code",
                                           lambda: (_ for _ in ()).throw(release.ReleaseError("unhealthy")))
                restarts = [call.args for call in commands.call_args_list if "restart" in call.args]
                self.assertEqual(restarts, [("systemctl", "restart", "rachel-planning")] * 2)
                self.assertEqual(db.read_bytes(), b"new business records must survive rollback")
                self.assertEqual(dropin.read_text() if dropin.exists() else None,
                                 "old planning code" if existing else None)

    def test_successful_activation_restarts_only_target(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(release, "run") as commands:
            self.assertEqual(release.activation("planning", Path(temp) / "planning.conf", "new", lambda: "verified"), "verified")
            self.assertEqual([call.args for call in commands.call_args_list],
                             [("systemctl", "daemon-reload"), ("systemctl", "restart", "rachel-planning")])

    def test_protected_service_change_is_detected_but_never_restarted(self):
        with patch.object(release, "identity", return_value={"MainPID": "99"}), patch.object(release, "run") as commands:
            with self.assertRaisesRegex(release.ReleaseError, "non-target"):
                release.assert_protected({"catalog": {"MainPID": "1"}, "replenishment": {"MainPID": "2"}}, "planning")
            commands.assert_not_called()

    def test_target_health_validation_is_paced_and_covers_current_worker(self):
        expected = dict(release_commit=NEW, release_generation=9, source_fingerprint="abc", workers=1)
        payload = dict(expected, status="ok", db_exists=True, application="planning_center", worker_pid=123)
        with patch.object(release, "identity", return_value={"MainPID": "123"}), \
                patch.object(release, "health", return_value=payload) as requests, \
                patch.object(release, "assert_protected", return_value={}), \
                patch.object(release.time, "sleep") as pause:
            result = release.verify_target("planning", expected, {}, timeout=1)
            self.assertEqual(result["workers"], [123])
            self.assertEqual(requests.call_count, 6)
            self.assertEqual(pause.call_count, 2)
            self.assertTrue(all(call.args[0] == "planning" for call in requests.call_args_list))

    def test_wrong_fingerprint_or_other_app_cannot_pass_public_verification(self):
        expected = dict(release_commit=NEW, release_generation=9, source_fingerprint="abc")
        payload = dict(expected, status="ok", db_exists=True, application="planning_center", worker_pid=123)
        publisher.verify_payload("planning", payload, expected)
        for key, value in [("source_fingerprint", "old"), ("application", "catalog_backend"), ("worker_pid", 0)]:
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                publisher.verify_payload("planning", dict(payload, **{key: value}), expected)

    def test_server_code_is_python39_compatible_and_contains_no_restore_path(self):
        for name in ["isolated_release.py", "release_scope.py", "install_isolated_dispatcher.py", "release_status.py"]:
            source = (ROOT / "scripts" / name).read_text()
            ast.parse(source, feature_version=(3, 9))
            self.assertNotIn("sqlite3.connect", source)
            self.assertNotIn("restore.sh", source)
            self.assertNotIn('"systemctl", "reload", "nginx"', source)
        self.assertNotIn("restore", (ROOT / "scripts/activate_production_release.sh").read_text())

    def test_old_style_publisher_exits_before_any_push(self):
        import subprocess
        result = subprocess.run(["bash", str(ROOT / "scripts/publish_verified_release.sh"), "tests.test_planning"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 78)

    def test_unavailable_origin_isolation_proof_blocks_before_any_push(self):
        def fake_git(*args):
            return {("branch", "--show-current"): "main", ("status", "--porcelain"): "",
                    ("rev-parse", "HEAD"): NEW, ("rev-list", "--count", NEW): "9",
                    ("ls-remote", "origin", "refs/heads/main"): OLD}.get(args, "")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "deploy").mkdir()
            for app in release.APPS:
                (root / "deploy/release-plan.json").write_text(json.dumps(plan(app)))
                with self.subTest(app=app), \
                        patch.object(publisher, "ROOT", root), \
                        patch.object(sys, "argv", ["publish", "--app", app, "tests.test_app"]), \
                        patch.object(publisher, "git", side_effect=fake_git) as commands, \
                        patch.object(publisher.subprocess, "run"), \
                        patch.object(publisher, "OriginStatus"), \
                        patch.object(publisher, "manifest", return_value={name: "a" * 64 for name in release.FILES}), \
                        patch.object(publisher, "readiness", side_effect=release.StatusError("origin proof unavailable")), \
                        redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(publisher.main(), 1)
                    self.assertIn("origin proof unavailable", errors.getvalue())
                    self.assertFalse(any("push" in call.args for call in commands.call_args_list))

    def exercise_dispatch(self, root, *, stale=False, staging_failure=False, verification_failure=False):
        automation = root / "automation"
        automation.mkdir()
        stage_dir = root / "planning/releases" / NEW / "app"
        stage_dir.mkdir(parents=True)
        expected = dict(release_commit=NEW, release_generation=9, source_fingerprint="abc", workers=1)
        protected = {app: {"MainPID": str(i + 1)} for i, app in enumerate(release.APPS)}

        def fake_git(*args, **kwargs):
            if args[0] == "rev-parse":
                return OLD if stale else NEW
            if args[0] == "rev-list":
                return "9"
            if args[0] == "diff":
                return "planning_center/web.py"
            if args[0] == "show":
                return json.dumps(plan()) if ":deploy/" in args[1] else "Release-App: planning"
            return ""

        with ExitStack() as stack:
            stack.enter_context(patch.object(release, "ROOT", root))
            stack.enter_context(patch.object(release, "AUTOMATION", automation))
            stack.enter_context(patch.object(release, "SYSTEMD_DIR", root / "systemd"))
            stack.enter_context(patch.object(release, "attest", return_value={
                "enabled_apps": ["planning"], "installed_hashes": {name: "a" * 64 for name in release.FILES}}))
            stack.enter_context(patch.object(release, "OriginStatus"))
            stack.enter_context(patch.object(release, "git", side_effect=fake_git))
            stack.enter_context(patch.object(release, "health", return_value={"release_commit": OLD, "status": "ok", "db_exists": True}))
            stack.enter_context(patch.object(release, "identity", side_effect=lambda app: protected[app]))
            commands = stack.enter_context(patch.object(release, "run", return_value=""))
            staging = stack.enter_context(patch.object(release, "stage", return_value=(stage_dir, stage_dir.parent / "venv/bin/python", expected)))
            verifier = stack.enter_context(patch.object(release, "verify_target", return_value={
                "workers": [2], "protected_after": {app: protected[app] for app in release.APPS if app != "planning"}}))
            if staging_failure:
                staging.side_effect = release.ReleaseError("candidate preflight failed")
            if verification_failure:
                verifier.side_effect = release.ReleaseError("public target failed")
            config = {"apps": {"planning": {"enabled": True, "baseline_commit": OLD, "baseline_generation": 8}}}
            error = None
            try:
                release.deploy(NEW, config)
            except release.ReleaseError as exc:
                error = exc
            return error, commands, staging

    def test_stale_webhook_cannot_stage_or_restart_any_application(self):
        with tempfile.TemporaryDirectory() as temp:
            error, commands, staging = self.exercise_dispatch(Path(temp), stale=True)
            self.assertIn("stale webhook", str(error))
            staging.assert_not_called()
            self.assertFalse(any("systemctl" in call.args for call in commands.call_args_list))

    def test_preflight_failure_leaves_all_running_services_and_state_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error, commands, _ = self.exercise_dispatch(root, staging_failure=True)
            self.assertIn("preflight failed", str(error))
            self.assertFalse(any("systemctl" in call.args for call in commands.call_args_list))
            self.assertFalse((root / "planning/state.json").exists())

    def test_failed_target_acceptance_cannot_advance_accepted_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error, commands, _ = self.exercise_dispatch(root, verification_failure=True)
            self.assertIn("public target failed", str(error))
            self.assertFalse((root / "planning/state.json").exists())
            self.assertFalse((root / "planning/current").exists())
            self.assertEqual([call.args for call in commands.call_args_list if "restart" in call.args],
                             [("systemctl", "restart", "rachel-planning")] * 2)

    def test_success_records_only_target_acceptance_and_private_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error, commands, _ = self.exercise_dispatch(root)
            self.assertIsNone(error)
            state = json.loads((root / "planning/state.json").read_text())
            self.assertEqual(state["accepted_commit"], NEW)
            self.assertEqual(set(state["protected_before"]), {"catalog", "replenishment"})
            self.assertEqual((root / "planning/current").resolve(), (root / "planning/releases" / NEW / "app").resolve())
            self.assertFalse((root / "catalog").exists())
            self.assertFalse((root / "replenishment").exists())
            self.assertEqual([call.args for call in commands.call_args_list if "restart" in call.args],
                             [("systemctl", "restart", "rachel-planning")])


if __name__ == "__main__":
    unittest.main()
