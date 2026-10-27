from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import isolated_release as release
import install_isolated_dispatcher as installer
import publish_isolated_release as publisher
import release_status as receipts

OLD = "a" * 40
NEW = "b" * 40
HASHES = {name: "c" * 64 for name in receipts.FILES}
DIGEST = receipts.manifest_digest(HASHES)
EXPECTED = dict(release_commit=NEW, release_generation=9, source_fingerprint="1234567890abcdef")
PROTECTED = {"catalog": {"MainPID": "101"}, "replenishment": {"MainPID": "102"}}
PROOF = dict(installed_hashes=HASHES, enabled_apps=list(release.APPS))


def accepted():
    client = Mock()
    receipts.post_accepted(client, "planning", NEW, dict(EXPECTED, workers=1), PROOF,
                           dict(workers=[123], protected_after=PROTECTED), PROTECTED)
    args = client.post.call_args.args
    return dict(id=1, context=args[1], status=args[2], description=args[3])


class StatusTests(unittest.TestCase):
    def test_credentials_stay_in_memory_and_noninteractive_requests_refuse_redirects(self):
        credential = SimpleNamespace(returncode=0, stdout="username=test\npassword=secret-value\n")
        with patch.object(receipts.subprocess, "run", return_value=credential) as command, \
                patch.object(receipts, "build_opener") as opener:
            client = receipts.OriginStatus()
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"[]"
            self.assertEqual(client.request("/statuses/" + NEW), [])
            self.assertNotIn("secret-value", repr(command.call_args))
            self.assertEqual(command.call_args.kwargs["env"]["GIT_ASKPASS"], "/usr/bin/false")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, receipts.API + "/statuses/" + NEW)
            self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        with self.assertRaises(receipts.StatusError):
            receipts.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")

    def test_api_errors_do_not_expose_credential_or_response_body(self):
        with patch.object(receipts.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="username=test\npassword=secret-value\n")), \
                patch.object(receipts, "build_opener") as opener:
            client = receipts.OriginStatus()
            opener.return_value.open.side_effect = receipts.HTTPError(
                receipts.API, 403, "secret-value", {}, None)
            with self.assertRaises(receipts.StatusError) as error:
                client.request("/statuses/" + NEW)
            self.assertEqual(str(error.exception), "origin status API HTTP 403")

    def test_server_uses_separate_root_only_status_token_without_git_password_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "origin-status.json"
            path.write_text(json.dumps(dict(username="writer", password="test-status-token")))
            with patch.object(receipts.Path, "lstat", return_value=SimpleNamespace(
                    st_mode=0o100600, st_uid=0)), patch.object(receipts.subprocess, "run") as helper:
                client = receipts.OriginStatus(credential_file=path)
                self.assertTrue(client._authorization.startswith("Basic "))
                helper.assert_not_called()
            for mode, uid in [(0o100644, 0), (0o100600, 501), (0o120600, 0)]:
                with patch.object(receipts.Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid)), \
                        self.assertRaises(receipts.StatusError):
                    receipts.OriginStatus(credential_file=path)

    def test_one_time_status_token_setup_never_changes_git_read_credential_or_prints_token(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(installer, "CONFIG", Path(temp) / "isolation.json"), \
                patch.object(installer.os, "geteuid", return_value=0), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(dict(username="writer", password="fake-token")))), \
                redirect_stdout(io.StringIO()) as output:
            installer.provision_status_credential()
            self.assertNotIn("fake-token", output.getvalue())
            path = Path(temp) / "origin-status.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(release.ReleaseError, "refusing overwrite"):
                installer.provision_status_credential()

    def test_latest_status_never_reuses_old_success_and_paginates(self):
        client = object.__new__(receipts.OriginStatus)
        older = dict(accepted(), id=10)
        newest = dict(older, id=11, status="pending")
        client.request = Mock(side_effect=[[older] + [dict(id=x, context="ci") for x in range(49)], [newest]])
        self.assertEqual(client.latest(NEW, "isolated-release/planning"), newest)
        self.assertIn("page=2", client.request.call_args.args[0])
        with self.assertRaises(receipts.StatusError):
            receipts.verify_receipt(newest, NEW, EXPECTED, DIGEST)

    def test_exact_commit_generation_fingerprint_manifest_and_isolation_are_all_required(self):
        status = accepted()
        receipts.verify_receipt(status, NEW, EXPECTED, DIGEST)
        for before, after in [(NEW, OLD), ("gen=9", "gen=8"), ("fp=123", "fp=999"),
                              ("isolation=2/2", "isolation=1/2"), (DIGEST, "f" * 64),
                              ("workers=1", "workers=0")]:
            with self.subTest(field=before), self.assertRaises(receipts.StatusError):
                receipts.verify_receipt(dict(status, description=status["description"].replace(before, after)),
                                        NEW, EXPECTED, DIGEST)

    def test_incomplete_worker_or_protected_evidence_never_posts_success(self):
        for evidence in [dict(workers=[], protected_after=PROTECTED),
                         dict(workers=[123], protected_after={}),
                         dict(workers=[123, 124], protected_after=PROTECTED)]:
            client = Mock()
            with self.assertRaises(receipts.StatusError):
                receipts.post_accepted(client, "planning", NEW, dict(EXPECTED, workers=1),
                                       PROOF, evidence, PROTECTED)
            client.post.assert_not_called()

    def test_readiness_requires_reviewed_installed_hashes_and_enabled_app(self):
        client = Mock()
        receipts.post_ready(client, OLD, PROOF)
        description = client.post.call_args.args[3]
        client.latest.return_value = dict(status="success", description=description)
        receipts.readiness(client, OLD, DIGEST, "planning")
        with self.assertRaises(receipts.StatusError):
            receipts.readiness(client, OLD, "f" * 64, "planning")
        client.latest.return_value = dict(status="success", description=description.replace(
            "catalog,planning,replenishment", "catalog"))
        with self.assertRaises(receipts.StatusError):
            receipts.readiness(client, OLD, DIGEST, "planning")

    def test_failed_receipt_stops_immediately_and_pending_never_passes(self):
        client = Mock()
        client.latest.return_value = dict(status="failure")
        with patch.object(receipts.time, "sleep") as pause, self.assertRaises(receipts.StatusError):
            receipts.wait_receipt(client, "planning", NEW, EXPECTED, DIGEST)
        pause.assert_not_called()
        client.latest.return_value = dict(status="pending")
        with patch.object(receipts.time, "monotonic", side_effect=[0, 0, 601]), \
                patch.object(receipts.time, "sleep"), self.assertRaises(receipts.StatusError):
            receipts.wait_receipt(client, "planning", NEW, EXPECTED, DIGEST)

    def exercise_publisher(self, *, rejected=False, unhealthy=False, proof_missing=False):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            (root / "deploy").mkdir()
            (root / "deploy/release-plan.json").write_text(json.dumps(dict(
                schema=1, application="planning", base_commit=OLD, repository_base_commit=OLD,
                reviewed_paths=[], database_migration=False)))
            pushes = []

            def fake_git(*args):
                if args == ("branch", "--show-current"):
                    return "main"
                if args == ("rev-parse", "HEAD"):
                    return NEW
                if args[:2] == ("rev-list", "--count"):
                    return "9"
                if args[:2] == ("show", "-s"):
                    return "Release-App: planning"
                if args[:2] == ("ls-remote", "origin"):
                    return (NEW if pushes else OLD) + "\trefs/heads/main"
                if args[:2] == ("ls-remote", "github"):
                    return NEW + "\trefs/heads/main"
                if args[0] == "push":
                    pushes.append(args[1])
                return ""

            commands = stack.enter_context(patch.object(publisher, "git", side_effect=fake_git))
            stack.enter_context(patch.object(publisher, "ROOT", root))
            stack.enter_context(patch.object(sys, "argv", ["publish", "--app", "planning", "tests.test_planning"]))
            local = stack.enter_context(patch.object(publisher.subprocess, "run"))
            stack.enter_context(patch.object(publisher, "manifest", return_value=HASHES))
            client = stack.enter_context(patch.object(publisher, "OriginStatus")).return_value
            client.latest.return_value = accepted()
            ready = stack.enter_context(patch.object(publisher, "readiness", return_value={"apps": "planning"}))
            if proof_missing:
                ready.side_effect = receipts.StatusError("not installed")
            stack.enter_context(patch.object(publisher, "review_paths", return_value=["planning_center/web.py"]))
            stack.enter_context(patch.object(publisher.importlib, "import_module", return_value=SimpleNamespace(
                PLANNING_SOURCE_FINGERPRINT=EXPECTED["source_fingerprint"])))
            waiter = stack.enter_context(patch.object(publisher, "wait_receipt"))
            if rejected:
                waiter.side_effect = receipts.StatusError("server rejected target")
            live = dict(EXPECTED, status="ok", db_exists=True, application="planning_center", worker_pid=123)
            stack.enter_context(patch.object(publisher, "health", side_effect=lambda *a, **k:
                dict(live, release_commit=OLD) if not pushes or unhealthy else live))
            stack.enter_context(patch.object(publisher.time, "sleep"))
            stack.enter_context(patch.object(publisher.time, "monotonic", side_effect=[0, 601]))
            errors = stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = publisher.main()
            self.last_publisher_error = errors.getvalue()
            self.assertFalse(any("ssh" in str(call) for call in local.call_args_list + commands.call_args_list))
            return result, pushes

    def test_routine_publish_has_no_ssh_and_github_follows_both_server_and_public_acceptance(self):
        self.assertEqual(self.exercise_publisher(), (0, ["origin", "github"]), self.last_publisher_error)
        self.assertEqual(self.exercise_publisher(proof_missing=True), (1, []))
        self.assertEqual(self.exercise_publisher(rejected=True), (1, ["origin"]))
        self.assertEqual(self.exercise_publisher(unhealthy=True), (1, ["origin"]))

    def test_server_refuses_modified_installed_dispatcher_before_any_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(release, "AUTOMATION", Path(temp)), \
                patch.object(release, "run", return_value="inactive") as commands:
            root = Path(temp)
            (root / "deploy.sh").write_text(installer.WRAPPER)
            installed = root / release.VERSION
            installed.mkdir()
            for name in receipts.FILES:
                (installed / name).write_text("reviewed")
            hashes = receipts.manifest(installed)
            (installed / "release_scope.py").write_text("changed after installation")
            with self.assertRaisesRegex(release.ReleaseError, "pinned"):
                release.attest(dict(installed_hashes=hashes, apps={}))
            self.assertFalse(any("restart" in call.args for call in commands.call_args_list))

    def test_duplicate_webhook_revalidates_workers_and_other_sites_without_restarting(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(release, "ROOT", Path(temp)), \
                patch.object(release, "verify_target") as verifier, patch.object(release, "run") as commands:
            root = Path(temp)
            (root / "planning").mkdir()
            runtime = root / "planning/releases" / NEW / "app"
            runtime.mkdir(parents=True)
            state = dict(accepted_commit=NEW, generation=9, source_fingerprint=EXPECTED["source_fingerprint"],
                         protected_before=PROTECTED, runtime=str(runtime))
            (root / "planning/state.json").write_text(json.dumps(state))
            verifier.return_value = dict(workers=[123], protected_after=PROTECTED)
            client = Mock()
            release.deploy_target(NEW, dict(apps={"planning": {}}), dict(application="planning"), PROOF, client, {})
            self.assertEqual(client.post.call_args.args[2], "success")
            commands.assert_not_called()
            verifier.side_effect = release.ReleaseError("protected process changed")
            client.reset_mock()
            with self.assertRaises(release.ReleaseError):
                release.deploy_target(NEW, dict(apps={"planning": {}}), dict(application="planning"), PROOF, client, {})
            client.post.assert_not_called()

    def test_multi_commit_push_cannot_hide_intermediate_other_app_changes_or_authorization(self):
        metadata = dict(application="planning", repository_base_commit=OLD, reviewed_paths=[])

        def fake_git(*args):
            if args[:2] == ("rev-list", "--reverse"):
                return OLD + "\n" + NEW
            if args[:2] == ("show", "-s"):
                return "Release-App: planning"
            if args[0] == "log":
                # Even if reverted in the last commit, it remains in push scope.
                return "planning_center/web.py\ncatalog_backend/web.py"
            return ""

        paths = release.review_paths(metadata, OLD, NEW, fake_git)
        self.assertIn("catalog_backend/web.py", paths)
        with self.assertRaisesRegex(release.ReleaseError, "another application"):
            release.validate_plan(dict(metadata, schema=1, base_commit=OLD, database_migration=False),
                                  "Release-App: planning", paths, OLD, ["planning"])
        with self.assertRaisesRegex(release.ReleaseError, "every commit"):
            release.review_paths(metadata, OLD, NEW, lambda *args:
                "Release-App: catalog" if args[:2] == ("show", "-s") else fake_git(*args))

    def test_dispatcher_upgrade_only_pauses_webhook_and_pins_reviewed_files(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            automation = root / "automation"
            installed = automation / release.VERSION
            installed.mkdir(parents=True)
            config_path = root / "etc/isolation.json"
            config_path.parent.mkdir()
            config = dict(version=release.VERSION, apps={app: {"enabled": True} for app in release.APPS})
            config_path.write_text(json.dumps(config))
            before = {}
            for app in release.APPS:
                runtime = root / app / "releases" / OLD / "app"
                runtime.mkdir(parents=True)
                (root / app / "current").symlink_to(runtime)
                (root / app / "state.json").write_text(json.dumps(dict(accepted_commit=OLD)))
                before[app] = dict(MainPID="123", WorkingDirectory=str(runtime))
            for name in receipts.FILES:
                (installed / name).write_text("old reviewed dispatcher")
            for name, value in {"AUTOMATION": automation, "ROOT": root, "CONFIG": config_path}.items():
                stack.enter_context(patch.object(installer, name, value))
            stack.enter_context(patch.object(installer.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(installer, "identity", side_effect=lambda app: before[app]))
            client = Mock()
            stack.enter_context(patch.object(installer, "status_preflight", return_value=(client, OLD)))
            stack.enter_context(patch.object(installer, "attest", return_value=dict(PROOF, services=before)))
            commands = stack.enter_context(patch.object(installer, "run"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            installer.upgrade()
            self.assertEqual([call.args for call in commands.call_args_list], [
                ("systemctl", "stop", "rachel-deploy-webhook"),
                ("systemctl", "start", "rachel-deploy-webhook")])
            updated = json.loads(config_path.read_text())
            self.assertEqual(updated["apps"], config["apps"])
            self.assertEqual(updated["installed_hashes"], receipts.manifest(ROOT / "scripts"))
            self.assertEqual(client.post.call_args.args[:3], (OLD, receipts.READY, "success"))
            for app in release.APPS:
                self.assertEqual((root / app / "current").resolve(), (root / app / "releases" / OLD / "app").resolve())

    def test_status_write_permission_preflight_failure_does_not_touch_installed_dispatcher(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            config_path = root / "isolation.json"
            config_path.write_text(json.dumps(dict(version=release.VERSION)))
            stack.enter_context(patch.object(installer, "CONFIG", config_path))
            stack.enter_context(patch.object(installer, "AUTOMATION", root))
            stack.enter_context(patch.object(installer.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(installer, "identity", return_value={}))
            stack.enter_context(patch.object(installer, "status_preflight", side_effect=receipts.StatusError("HTTP 403")))
            commands = stack.enter_context(patch.object(installer, "run"))
            with self.assertRaises(receipts.StatusError):
                installer.upgrade()
            commands.assert_not_called()
            self.assertFalse((root / release.VERSION).exists())


if __name__ == "__main__":
    unittest.main()
