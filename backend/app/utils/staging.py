"""Short-lived staging records for the Kestra prepare/apply split.

The backend needs the raw inbound email (sender, attachment metadata, phone)
when the apply endpoint runs, but that data must not travel through Kestra.
A staging file keeps it on the backend only. Files are keyed by an opaque hash,
are rewritten to a minimal result after apply, and are cleaned up after a week.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def digest_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _path(root: Path | str, namespace: str, key: str) -> Path:
    safe = digest_key(key)
    return Path(root) / ".staging" / namespace / f"{safe}.json"


def write_stage(
    root: Path | str, namespace: str, key: str, payload: dict[str, Any]
) -> Path:
    path = _path(root, namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    os.replace(temporary, path)
    return path


def read_stage(root: Path | str, namespace: str, key: str) -> dict[str, Any] | None:
    path = _path(root, namespace, key)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def clear_stage(root: Path | str, namespace: str, key: str) -> None:
    path = _path(root, namespace, key)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def cleanup_stage(
    root: Path | str, namespace: str, *, max_age_seconds: int = 7 * 24 * 3600
) -> int:
    """Remove stale staging files. Returns the number removed."""
    folder = Path(root) / ".staging" / namespace
    if not folder.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in folder.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
