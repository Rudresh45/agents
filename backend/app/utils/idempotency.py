"""Filesystem claims for outbound mail that must be sent at most once.

A claim is written *before* SMTP is called. A crash after the claim can lose
one email, but a Kestra retry can never send a duplicate. The caller removes
the claim only when the send fails, so the next attempt can try again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def marker_path(root: Path | str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return Path(root) / ".markers" / f"{digest}.sent"


def claim_marker(path: Path) -> bool:
    """Return True when this call created the claim, False when it existed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, b"claimed\n")
    finally:
        os.close(descriptor)
    return True


def release_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
