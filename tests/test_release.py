from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_backend.release import (
    ReleaseCheckoutError,
    catalog_release_commit,
    catalog_release_generation,
    stale_release_reason,
    sync_checkout_to_origin_main,
)


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def make_release_repositories(root: Path) -> tuple[Path, Path, Path]:
    remote = root / "origin.git"
    repo = root / "repo"
    updater = root / "updater"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        capture_output=True,
        check=True,
        text=True,
    )
    run_git(repo, "config", "user.name", "Release Test")
    run_git(repo, "config", "user.email", "release-test@example.invalid")
    (repo / "source.txt").write_text("release 1\n", encoding="utf-8")
    run_git(repo, "add", "source.txt")
    run_git(repo, "commit", "-m", "release 1")
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "push", "--set-upstream", "origin", "main")
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        capture_output=True,
        check=True,
        text=True,
    )
    run_git(updater, "config", "user.name", "Release Test")
    run_git(updater, "config", "user.email", "release-test@example.invalid")
    return remote, repo, updater


class CatalogReleaseTests(unittest.TestCase):
    def test_sync_checkout_fast_forwards_clean_main_to_origin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repo, updater = make_release_repositories(Path(temp_dir))
            (updater / "source.txt").write_text("release 2\n", encoding="utf-8")
            run_git(updater, "add", "source.txt")
            run_git(updater, "commit", "-m", "release 2")
            run_git(updater, "push", "origin", "main")
            target_commit = run_git(updater, "rev-parse", "HEAD")

            synced_commit = sync_checkout_to_origin_main(repo, target_commit)

            self.assertEqual(synced_commit, target_commit)
            self.assertEqual(run_git(repo, "rev-parse", "HEAD"), target_commit)
            self.assertEqual((repo / "source.txt").read_text(encoding="utf-8"), "release 2\n")

    def test_sync_refuses_dirty_checkout_without_overwriting_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repo, updater = make_release_repositories(Path(temp_dir))
            (updater / "source.txt").write_text("release 2\n", encoding="utf-8")
            run_git(updater, "add", "source.txt")
            run_git(updater, "commit", "-m", "release 2")
            run_git(updater, "push", "origin", "main")
            original_head = run_git(repo, "rev-parse", "HEAD")
            (repo / "source.txt").write_text("local edit\n", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseCheckoutError, "local changes"):
                sync_checkout_to_origin_main(repo)

            self.assertEqual(run_git(repo, "rev-parse", "HEAD"), original_head)
            self.assertEqual((repo / "source.txt").read_text(encoding="utf-8"), "local edit\n")

    def test_sync_refuses_diverged_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repo, updater = make_release_repositories(Path(temp_dir))
            (repo / "source.txt").write_text("local release\n", encoding="utf-8")
            run_git(repo, "add", "source.txt")
            run_git(repo, "commit", "-m", "local release")
            local_head = run_git(repo, "rev-parse", "HEAD")
            (updater / "source.txt").write_text("remote release\n", encoding="utf-8")
            run_git(updater, "add", "source.txt")
            run_git(updater, "commit", "-m", "remote release")
            run_git(updater, "push", "origin", "main")

            with self.assertRaisesRegex(ReleaseCheckoutError, "not an ancestor"):
                sync_checkout_to_origin_main(repo)

            self.assertEqual(run_git(repo, "rev-parse", "HEAD"), local_head)

    def test_sync_refuses_mismatched_pinned_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repo, _ = make_release_repositories(Path(temp_dir))

            with self.assertRaisesRegex(ReleaseCheckoutError, "not expected release"):
                sync_checkout_to_origin_main(repo, "0" * 40)

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
