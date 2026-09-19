from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_backend.release import catalog_release_commit, catalog_release_generation, stale_release_reason


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


if __name__ == "__main__":
    unittest.main()
