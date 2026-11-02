from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.exceptions import ValidationFailedError
from app.utils.storage import Storage

PDF_BYTES = b"%PDF-1.4\n% test resume\n"


def _storage(tmp_path: Path, max_mb: int = 1) -> Storage:
    return Storage(Settings(upload_dir=tmp_path, max_upload_mb=max_mb))


def test_saves_pdf_under_candidate_folder(tmp_path: Path):
    storage = _storage(tmp_path)
    stored = storage.save(PDF_BYTES, "resume.pdf", "candidate-1")
    assert stored.relative_path.startswith("candidate-1/")
    assert stored.relative_path.endswith(".pdf")
    assert stored.size == len(PDF_BYTES)
    assert storage.read(stored.relative_path) == PDF_BYTES
    assert (tmp_path / stored.relative_path).is_file()


def test_rejects_wrong_extension(tmp_path: Path):
    storage = _storage(tmp_path)
    with pytest.raises(ValidationFailedError):
        storage.save(b"hello", "resume.exe", "c1")


def test_rejects_extension_mismatch(tmp_path: Path):
    storage = _storage(tmp_path)
    with pytest.raises(ValidationFailedError):
        storage.save(b"PK\x03\x04 not really docx", "resume.pdf", "c1")


def test_rejects_empty_file(tmp_path: Path):
    storage = _storage(tmp_path)
    with pytest.raises(ValidationFailedError):
        storage.save(b"", "resume.pdf", "c1")


def test_rejects_oversized_file(tmp_path: Path):
    storage = _storage(tmp_path, max_mb=1)
    with pytest.raises(ValidationFailedError):
        storage.save(b"%PDF-" + b"x" * (1024 * 1024 + 10), "resume.pdf", "c1")


def test_rejects_unsafe_candidate_id(tmp_path: Path):
    storage = _storage(tmp_path)
    with pytest.raises(ValidationFailedError):
        storage.save(PDF_BYTES, "resume.pdf", "../escape")


def test_resolve_blocks_traversal(tmp_path: Path):
    storage = _storage(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValidationFailedError):
        storage.resolve("../secret.txt")


def test_delete_removes_file_and_empty_folder(tmp_path: Path):
    storage = _storage(tmp_path)
    stored = storage.save(PDF_BYTES, "resume.pdf", "c1")
    storage.delete(stored.relative_path)
    assert not (tmp_path / stored.relative_path).exists()
    assert not (tmp_path / "c1").exists()


def test_delete_is_idempotent(tmp_path: Path):
    storage = _storage(tmp_path)
    storage.delete("c1/missing.pdf")
