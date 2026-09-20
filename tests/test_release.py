from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_backend.release import (
    catalog_post_release_verification,
    catalog_release_commit,
    catalog_release_generation,
    post_release_verification_is_current,
    stale_release_reason,
)


class CatalogReleaseTests(unittest.TestCase):
    def test_packaged_release_manifest_wins_over_stale_git_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            (root / ".release-commit").write_text("a" * 40, encoding="utf-8")

            with (
                patch.dict(os.environ, {}, clear=False),
                patch(
                    "catalog_backend.release.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="b" * 40),
                ) as git_run,
            ):
                os.environ.pop("CATALOG_RELEASE_COMMIT", None)
                self.assertEqual(catalog_release_commit(root), "a" * 40)
                git_run.assert_not_called()

    def test_live_checkout_without_manifest_falls_back_to_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()

            with (
                patch.dict(os.environ, {}, clear=False),
                patch(
                    "catalog_backend.release.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="b" * 40),
                ),
            ):
                os.environ.pop("CATALOG_RELEASE_COMMIT", None)
                self.assertEqual(catalog_release_commit(root), "b" * 40)

    def test_packaged_release_uses_manifest_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".release-commit").write_text("c" * 40, encoding="utf-8")

            with (
                patch.dict(os.environ, {}, clear=False),
                patch("catalog_backend.release.subprocess.run") as git_run,
            ):
                os.environ.pop("CATALOG_RELEASE_COMMIT", None)
                self.assertEqual(catalog_release_commit(root), "c" * 40)
                git_run.assert_not_called()

    def test_explicit_release_commit_remains_highest_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            (root / ".release-commit").write_text("a" * 40, encoding="utf-8")

            with patch.dict(os.environ, {"CATALOG_RELEASE_COMMIT": "D" * 40}):
                self.assertEqual(catalog_release_commit(root), "d" * 40)

    def test_packaged_generation_wins_over_stale_git_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            (root / ".release-generation").write_text("42", encoding="utf-8")

            with (
                patch.dict(os.environ, {}, clear=False),
                patch("catalog_backend.release.subprocess.run") as git_run,
            ):
                os.environ.pop("CATALOG_RELEASE_GENERATION", None)
                self.assertEqual(catalog_release_generation(root), 42)
                git_run.assert_not_called()

    def test_live_checkout_generation_falls_back_to_git_history_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()

            with (
                patch.dict(os.environ, {}, clear=False),
                patch(
                    "catalog_backend.release.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="43"),
                ),
            ):
                os.environ.pop("CATALOG_RELEASE_GENERATION", None)
                self.assertEqual(catalog_release_generation(root), 43)

    def test_explicit_release_generation_remains_highest_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".release-generation").write_text("42", encoding="utf-8")

            with patch.dict(os.environ, {"CATALOG_RELEASE_GENERATION": "44"}):
                self.assertEqual(catalog_release_generation(root), 44)

    def test_older_candidate_is_rejected(self):
        reason = stale_release_reason(44, "b" * 40, 43, "a" * 40)
        self.assertIn("newer than candidate", reason or "")

    def test_same_generation_with_different_commit_is_rejected(self):
        reason = stale_release_reason(44, "b" * 40, 44, "a" * 40)
        self.assertIn("already occupied", reason or "")

    def test_newer_candidate_is_allowed(self):
        self.assertIsNone(stale_release_reason(44, "a" * 40, 45, "b" * 40))

    def test_delayed_verification_state_is_read_and_matches_current_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "post-release.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "verified_at": "2026-09-20T02:30:00Z",
                        "release_commit": "a" * 40,
                        "release_generation": 110,
                        "build_version": "release-watchdog-v8",
                        "source_fingerprint": "0123456789abcdef",
                        "check_count": 256,
                        "expected_workers": 8,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CATALOG_POST_RELEASE_STATE": str(state_path)}):
                verification = catalog_post_release_verification()

        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["check_count"], 256)
        self.assertTrue(
            post_release_verification_is_current(
                verification,
                release_commit="a" * 40,
                release_generation=110,
                build_version="release-watchdog-v8",
                source_fingerprint="0123456789abcdef",
            )
        )
        self.assertFalse(
            post_release_verification_is_current(
                verification,
                release_commit="b" * 40,
                release_generation=111,
                build_version="release-watchdog-v9",
                source_fingerprint="fedcba9876543210",
            )
        )

    def test_missing_delayed_verification_state_remains_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.json"
            with patch.dict(os.environ, {"CATALOG_POST_RELEASE_STATE": str(missing_path)}):
                verification = catalog_post_release_verification()

        self.assertEqual(verification["status"], "pending")
        self.assertIsNone(verification["verified_at"])
        self.assertEqual(verification["release_commit"], "unknown")


if __name__ == "__main__":
    unittest.main()
