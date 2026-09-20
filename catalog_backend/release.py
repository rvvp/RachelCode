from __future__ import annotations

import hashlib
import json
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


def catalog_post_release_verification(root_dir: str | Path | None = None) -> dict:
    """Read the latest delayed public verification without caching it in workers."""
    root = Path(root_dir).resolve() if root_dir else Path(__file__).resolve().parent.parent
    configured_path = str(os.environ.get("CATALOG_POST_RELEASE_STATE") or "").strip()
    state_path = Path(configured_path).expanduser() if configured_path else root / ".post-release-verification.json"
    default_state = {
        "status": "pending",
        "verified_at": None,
        "release_commit": "unknown",
        "release_generation": 0,
        "build_version": "",
        "source_fingerprint": "",
        "check_count": 0,
        "expected_workers": 0,
    }
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default_state
    if not isinstance(payload, dict):
        return default_state
    status = str(payload.get("status") or "").strip().lower()
    return {
        "status": status if status in {"passed", "failed"} else "pending",
        "verified_at": str(payload.get("verified_at") or "").strip() or None,
        "release_commit": _normalized_commit(payload.get("release_commit")) or "unknown",
        "release_generation": _normalized_generation(payload.get("release_generation")) or 0,
        "build_version": str(payload.get("build_version") or "").strip(),
        "source_fingerprint": str(payload.get("source_fingerprint") or "").strip(),
        "check_count": _normalized_generation(payload.get("check_count")) or 0,
        "expected_workers": _normalized_generation(payload.get("expected_workers")) or 0,
    }


def post_release_verification_is_current(
    verification: dict,
    *,
    release_commit: str,
    release_generation: int,
    build_version: str,
    source_fingerprint: str,
) -> bool:
    """Return whether the delayed verification passed for the running release."""
    return bool(
        verification.get("status") == "passed"
        and verification.get("release_commit") == release_commit
        and verification.get("release_generation") == release_generation
        and verification.get("build_version") == build_version
        and verification.get("source_fingerprint") == source_fingerprint
    )


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
        root_dir / "scripts" / "run_post_release_verification.sh",
        root_dir / "scripts" / "verify_public_deployment.sh",
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
