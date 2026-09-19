from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def catalog_release_commit(root_dir: str | Path | None = None) -> str:
    """Return the repository commit represented by this deployed release."""
    root = Path(root_dir).resolve() if root_dir else Path(__file__).resolve().parent.parent
    candidates = [
        str(os.environ.get("CATALOG_RELEASE_COMMIT") or "").strip(),
    ]
    manifest_path = root / ".release-commit"
    if manifest_path.is_file():
        try:
            candidates.append(manifest_path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    for candidate in candidates:
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate):
            return candidate.lower()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        candidate = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate):
            return candidate.lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


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
CATALOG_RUNTIME_MODE = str(os.environ.get("CATALOG_RUNTIME_MODE") or "development").strip().lower()
