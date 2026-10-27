from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("release_scope", ROOT / "scripts/release_scope.py")
release_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_scope)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_planning_only_change_never_targets_catalog_or_replenishment(self):
        report = release_scope.build_report(["planning_center/web.py", "tests/test_planning.py"], ["planning"])
        self.assertTrue(report["scope_review_passed"])
        self.assertEqual(report["changed_applications"], ["planning"])
        self.assertEqual(report["must_not_restart"], ["catalog", "replenishment"])
        self.assertFalse(report["production_release_authorized"])

    def test_each_application_has_separate_ownership(self):
        for app, path in [("catalog", "catalog_backend/web.py"),
                          ("planning", "planning_app.py"),
                          ("replenishment", "replenishment_center/db.py")]:
            with self.subTest(app=app):
                report = release_scope.build_report([path], [app])
                self.assertTrue(report["scope_review_passed"])
                self.assertEqual(report["changed_applications"], [app])
                self.assertEqual(len(report["must_not_restart"]), 2)

    def test_no_default_all_applications_target(self):
        report = release_scope.build_report(["planning_center/web.py"], [])
        self.assertIn("explicit_application_target_required", report["issues"])
        self.assertEqual(report["must_not_restart"], ["catalog", "planning", "replenishment"])

    def test_shared_source_requires_review_but_does_not_expand_target(self):
        report = release_scope.build_report(["catalog_backend/release.py"], ["planning"])
        self.assertFalse(report["scope_review_passed"])
        self.assertEqual(report["requested_apps"], ["planning"])
        self.assertEqual(report["compatibility_test_consumers"], ["catalog", "planning", "replenishment"])
        self.assertIn("catalog", report["must_not_restart"])

    def test_runtime_and_sensitive_paths_are_forbidden(self):
        for path in ["planning_data/planning.db", "data/catalog.db", ".env",
                     ".env.production", "backups/catalog.db", "logs/server.log",
                     "secrets/private.pem", "data/uploads/image.jpg", ".release-commit"]:
            with self.subTest(path=path):
                report = release_scope.build_report([path], ["planning"])
                self.assertIn("runtime_or_sensitive_files_forbidden", report["issues"])

    def test_unmapped_and_path_traversal_fail_closed(self):
        for path in ["new_service/main.py", "../planning_center/web.py", "/planning_center/web.py"]:
            with self.subTest(path=path):
                report = release_scope.build_report([path], ["planning"])
                self.assertIn("unmapped_paths_require_review", report["issues"])

    def test_other_app_change_is_not_silently_included(self):
        report = release_scope.build_report(["catalog_backend/web.py"], ["planning"])
        self.assertIn("changed_applications_outside_authorized_scope", report["issues"])
        self.assertEqual(report["requested_apps"], ["planning"])

    def test_deployment_infrastructure_requires_manual_review(self):
        report = release_scope.build_report(["scripts/activate_production_release.sh"], ["planning"])
        self.assertIn("release_infrastructure_requires_isolation_review", report["issues"])
        self.assertFalse(report["production_release_authorized"])

    def test_documents_do_not_require_all_sites_to_deploy(self):
        report = release_scope.build_report(["AGENTS.md", "APPLICATION_BOUNDARIES.md"], ["planning"])
        self.assertEqual(report["changed_applications"], [])
        self.assertEqual(report["compatibility_test_consumers"], [])
        self.assertFalse(report["production_release_authorized"])

    def test_both_legacy_entrypoints_exit_before_network_or_mutation(self):
        for script in ["publish_verified_release.sh", "activate_production_release.sh"]:
            with self.subTest(script=script):
                result = subprocess.run(
                    ["/bin/bash", str(ROOT / "scripts" / script), "tests.test_release"],
                    env={"PATH": "/nonexistent"}, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
                self.assertIn("禁用", result.stderr)

    def test_document_requires_data_recovery_separate_from_code_rollback(self):
        doc = (ROOT / "APPLICATION_BOUNDARIES.md").read_text(encoding="utf-8")
        self.assertIn("绝不把发布前的旧数据库自动覆盖回正式库", doc)
        self.assertIn("不代表服务器已完成隔离", doc)
        self.assertIn("/data/rachelcode/catalog", doc)


if __name__ == "__main__":
    unittest.main()
