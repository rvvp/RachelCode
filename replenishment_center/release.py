from __future__ import annotations

import hashlib
import os
from pathlib import Path

from catalog_backend.release import (
    PROCESS_STARTED_AT,
    catalog_release_commit,
    catalog_release_generation,
)


REPLENISHMENT_BUILD_VERSION = "2026.09.24-excel-snapshot-v1"


def replenishment_source_fingerprint() -> str:
    """Identify the exact replenishment application source loaded by the process."""
    root_dir = Path(__file__).resolve().parent.parent
    source_paths = [
        root_dir / "replenishment_app.py",
        root_dir / "requirements.txt",
        root_dir / "deploy" / "systemd" / "rachel-replenishment.service",
        root_dir / "scripts" / "activate_production_release.sh",
        root_dir / "scripts" / "package_release.sh",
        root_dir / "scripts" / "publish_verified_release.sh",
        root_dir / "scripts" / "verify_replenishment_deployment.sh",
    ]
    source_paths.extend(sorted((root_dir / "replenishment_center").glob("*.py")))
    digest = hashlib.sha256()
    for source_path in source_paths:
        if not source_path.is_file():
            continue
        digest.update(source_path.relative_to(root_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


REPLENISHMENT_SOURCE_FINGERPRINT = replenishment_source_fingerprint()
REPLENISHMENT_RELEASE_COMMIT = catalog_release_commit()
REPLENISHMENT_RELEASE_GENERATION = catalog_release_generation()
REPLENISHMENT_RUNTIME_MODE = str(
    os.environ.get("REPLENISH_RUNTIME_MODE") or "development"
).strip().lower()
