"""Small cross-process lock for named workflow steps.

Kestra can retry a task while an earlier attempt is still finishing. The lock
file is created next to uploads, contains no candidate data, and is released in
``finally`` so a failed request does not block the next attempt.
"""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import suppress
from pathlib import Path


class StepLockBusyError(RuntimeError):
    """Raised when another process already holds the same step lock."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Step '{name}' is already running")


class StepLock:
    """Advisory lock backed by an exclusive lock file.

    This is intentionally simple: one backend container or several containers
    sharing the upload volume can coordinate without adding a database table.
    A crashed process leaves a stale file, which is reclaimed after
    ``stale_seconds``.
    """

    def __init__(
        self,
        directory: Path | str,
        name: str,
        *,
        stale_seconds: int = 3600,
    ) -> None:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        self.directory = Path(directory)
        self.path = self.directory / f"{digest}.lock"
        self.name = name
        self.stale_seconds = stale_seconds
        self._held = False

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            self._held = True
            return

        if self._is_stale():
            with suppress(FileNotFoundError):
                self.path.unlink()
            if self._try_create():
                self._held = True
                return

        raise StepLockBusyError(self.name)

    def release(self) -> None:
        if not self._held:
            return
        with suppress(FileNotFoundError):
            self.path.unlink()
        self._held = False

    def _try_create(self) -> bool:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        try:
            payload = f"{os.getpid()} {time.time():.3f}\n".encode("ascii")
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return True

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > self.stale_seconds

    def __enter__(self) -> StepLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.release()
