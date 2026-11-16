"""Immutable provenance for the Python sources loaded by a service process."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path


def _capture_runtime() -> dict[str, str | int]:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return {
        'source_revision': digest.hexdigest()[:16],
        'loaded_at': datetime.now(timezone.utc).isoformat(),
        'pid': os.getpid(),
    }


_LOADED_RUNTIME = _capture_runtime()


def runtime_info() -> dict[str, str | int]:
    return dict(_LOADED_RUNTIME)
