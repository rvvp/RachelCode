from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_backend.release import catalog_release_commit


class CatalogReleaseTests(unittest.TestCase):
    def test_live_checkout_uses_head_instead_of_stale_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            (root / ".release-commit").write_text("a" * 40, encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
