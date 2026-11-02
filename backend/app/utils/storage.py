"""Upload storage with type, size and path-traversal protection."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationFailedError

ALLOWED_EXTENSIONS = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    size: int
    sha256: str


class Storage:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = Path(self.settings.upload_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _validate(self, data: bytes, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValidationFailedError(
                "Only PDF and DOCX files are accepted", details={"extension": suffix}
            )
        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        if not data:
            raise ValidationFailedError("The uploaded file is empty")
        if len(data) > max_bytes:
            raise ValidationFailedError(
                "The uploaded file is too large",
                details={"max_mb": self.settings.max_upload_mb},
            )
        magic = ALLOWED_EXTENSIONS[suffix]
        if not data.startswith(magic):
            raise ValidationFailedError(
                "The file content does not match its extension",
                details={"extension": suffix},
            )
        return suffix

    def save(self, data: bytes, filename: str, candidate_id: str) -> StoredFile:
        suffix = self._validate(data, filename)
        if not _SAFE_ID.match(candidate_id):
            raise ValidationFailedError("Invalid candidate identifier")

        folder = self.root / candidate_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{uuid.uuid4().hex}{suffix}"
        target.write_bytes(data)

        return StoredFile(
            relative_path=f"{candidate_id}/{target.name}",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValidationFailedError("Invalid file path")
        if not candidate.is_file():
            raise ValidationFailedError("File not found")
        return candidate

    def read(self, relative_path: str) -> bytes:
        return self.resolve(relative_path).read_bytes()

    def delete(self, relative_path: str) -> None:
        try:
            path = self.resolve(relative_path)
        except ValidationFailedError:
            return
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
