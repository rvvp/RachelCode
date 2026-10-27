from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalized_commit(value: object) -> str | None:
    candidate = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate):
        return candidate.lower()
    return None


def _normalized_generation(value: object) -> int | None:
    try:
        generation = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return generation if generation > 0 else None


class ReleaseCheckoutError(RuntimeError):
    pass


def sync_checkout_to_origin_main(
    root_dir: str | Path,
    expected_commit: str | None = None,
) -> str:
    """Fast-forward a clean production checkout to origin/main and return its SHA."""
    root = Path(root_dir).resolve()

    def git(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                check=False,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseCheckoutError(f"git {' '.join(args)} failed: {exc}") from exc
        if result.returncode and not allow_failure:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise ReleaseCheckoutError(f"git {' '.join(args)} failed: {detail}")
        return result

    checkout_root = git("rev-parse", "--show-toplevel").stdout.strip()
    if not checkout_root or Path(checkout_root).resolve() != root:
        raise ReleaseCheckoutError("activation path is not the Git checkout root")

    branch = git("branch", "--show-current").stdout.strip()
    if branch != "main":
        raise ReleaseCheckoutError(f"production checkout is on {branch or 'detached HEAD'}, expected main")

    status = git("status", "--porcelain").stdout.strip()
    if status:
        raise ReleaseCheckoutError("production checkout has local changes; refusing to overwrite them")

    pinned_commit = _normalized_commit(expected_commit)
    if expected_commit and not pinned_commit:
        raise ReleaseCheckoutError("expected release commit is not a valid Git SHA")

    git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    target_commit = _normalized_commit(git("rev-parse", "refs/remotes/origin/main").stdout)
    if not target_commit:
        raise ReleaseCheckoutError("origin/main did not provide a valid Git SHA")
    if pinned_commit and target_commit != pinned_commit:
        raise ReleaseCheckoutError(
            f"origin/main is {target_commit}, not expected release {pinned_commit}"
        )

    current_commit = _normalized_commit(git("rev-parse", "HEAD").stdout)
    if not current_commit:
        raise ReleaseCheckoutError("production checkout HEAD is not a valid Git SHA")
    if current_commit != target_commit:
        is_fast_forward = git(
            "merge-base", "--is-ancestor", current_commit, target_commit, allow_failure=True
        )
        if is_fast_forward.returncode != 0:
            raise ReleaseCheckoutError(
                f"production HEAD {current_commit} is not an ancestor of origin/main {target_commit}"
            )
        git("merge", "--ff-only", target_commit)

    activated_commit = _normalized_commit(git("rev-parse", "HEAD").stdout)
    if activated_commit != target_commit:
        raise ReleaseCheckoutError(
            f"checkout is {activated_commit or 'unknown'} after sync, expected {target_commit}"
        )
    return target_commit


def catalog_release_commit(root_dir: str | Path | None = None) -> str:
    """Return the repository commit represented by this deployed release."""
    root = Path(root_dir).resolve() if root_dir else Path(__file__).resolve().parent.parent

    configured_commit = _normalized_commit(os.environ.get("CATALOG_RELEASE_COMMIT"))
    if configured_commit:
        return configured_commit

    # The deployment manifest describes the files in a packaged release. It
    # must win over Git metadata because the runtime directory can retain an
    # older .git directory while a new release package is being activated.
    manifest_path = root / ".release-commit"
    if manifest_path.is_file():
        try:
            manifest_commit = _normalized_commit(manifest_path.read_text(encoding="utf-8"))
            if manifest_commit:
                return manifest_commit
        except OSError:
            pass

    # Direct Git deployments refresh .release-commit before activation. Git is
    # therefore only a fallback for checkouts that have not been packaged yet.
    if (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            git_commit = _normalized_commit(result.stdout) if result.returncode == 0 else None
            if git_commit:
                return git_commit
        except (OSError, subprocess.SubprocessError):
            pass
    return "unknown"


def catalog_release_generation(root_dir: str | Path | None = None) -> int:
    """Return a monotonic Git history number for stale-release protection."""
    root = Path(root_dir).resolve() if root_dir else Path(__file__).resolve().parent.parent

    configured_generation = _normalized_generation(os.environ.get("CATALOG_RELEASE_GENERATION"))
    if configured_generation:
        return configured_generation

    manifest_path = root / ".release-generation"
    if manifest_path.is_file():
        try:
            manifest_generation = _normalized_generation(manifest_path.read_text(encoding="utf-8"))
            if manifest_generation:
                return manifest_generation
        except OSError:
            pass

    if (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            git_generation = _normalized_generation(result.stdout) if result.returncode == 0 else None
            if git_generation:
                return git_generation
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def stale_release_reason(
    current_generation: object,
    current_commit: object,
    candidate_generation: object,
    candidate_commit: object,
) -> str | None:
    """Explain why a candidate must not replace the running release."""
    current_number = _normalized_generation(current_generation) or 0
    candidate_number = _normalized_generation(candidate_generation) or 0
    current_revision = _normalized_commit(current_commit) or "unknown"
    candidate_revision = _normalized_commit(candidate_commit) or "unknown"
    if current_number > candidate_number:
        return (
            f"current release {current_revision}/{current_number} is newer than "
            f"candidate {candidate_revision}/{candidate_number}"
        )
    if current_number > 0 and current_number == candidate_number and current_revision != candidate_revision:
        return (
            f"release generation {current_number} is already occupied by "
            f"{current_revision}, not {candidate_revision}"
        )
    return None


def catalog_source_fingerprint() -> str:
    """Identify the exact application source loaded by the current process."""
    root_dir = Path(__file__).resolve().parent.parent
    source_paths = [
        root_dir / "app.py",
        root_dir / "catalog_wsgi.py",
        root_dir / "requirements.txt",
        root_dir / "deploy" / "systemd" / "rachel-catalog.service",
        root_dir / "deploy" / "systemd" / "rachel-catalog-post-release-verify.service",
        root_dir / "deploy" / "systemd" / "rachel-catalog-post-release-verify.timer",
        root_dir / "scripts" / "activate_production_release.sh",
        root_dir / "scripts" / "publish_verified_release.sh",
        root_dir / "scripts" / "verify_public_deployment.sh",
        root_dir / "scripts" / "verify_replenishment_deployment.sh",
    ]
    source_paths.extend(sorted((root_dir / "catalog_backend").glob("*.py")))
    source_paths.extend(sorted((root_dir / "catalog_backend" / "assets").glob("**/*")))
    digest = hashlib.sha256()
    for source_path in source_paths:
        if not source_path.is_file():
            continue
        digest.update(source_path.relative_to(root_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


CATALOG_SOURCE_FINGERPRINT = catalog_source_fingerprint()
CATALOG_RELEASE_COMMIT = catalog_release_commit()
CATALOG_RELEASE_GENERATION = catalog_release_generation()
CATALOG_RUNTIME_MODE = str(os.environ.get("CATALOG_RUNTIME_MODE") or "development").strip().lower()
