from __future__ import annotations

import hashlib
from pathlib import Path

from catalog_backend.release import catalog_release_commit, catalog_release_generation


def planning_source_fingerprint() -> str:
    root = Path(__file__).resolve().parent.parent
    paths = [root / "planning_app.py", root / "requirements.txt"]
    paths.extend(sorted((root / "planning_center").glob("*.py")))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


PLANNING_SOURCE_FINGERPRINT = planning_source_fingerprint()
PLANNING_RELEASE_COMMIT = catalog_release_commit()
PLANNING_RELEASE_GENERATION = catalog_release_generation()
