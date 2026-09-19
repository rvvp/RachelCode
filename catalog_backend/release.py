from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path


PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def catalog_source_fingerprint() -> str:
    """Identify the exact application source loaded by the current process."""
    root_dir = Path(__file__).resolve().parent.parent
    source_paths = [root_dir / "app.py", root_dir / "catalog_wsgi.py"]
    source_paths.extend(sorted((root_dir / "catalog_backend").glob("*.py")))
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
CATALOG_RUNTIME_MODE = str(os.environ.get("CATALOG_RUNTIME_MODE") or "development").strip().lower()
