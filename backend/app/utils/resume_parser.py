"""Resume text extraction for PDF and DOCX, with an optional OCR fallback."""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


class ParsedResume(BaseModel):
    text: str = ""
    method: str = "failed"
    page_count: int = 0
    warnings: list[str] = []


def _parse_pdf(path: Path) -> tuple[str, int]:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n\n".join(pages).strip(), len(pages)


def _parse_docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def _ocr_pdf(path: Path) -> str | None:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return None
    try:
        images = convert_from_path(str(path))
        return "\n".join(pytesseract.image_to_string(image) for image in images).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr_failed", error=str(exc)[:200])
        return None


def parse_resume(path: Path | str, *, min_chars: int | None = None) -> ParsedResume:
    settings = get_settings()
    threshold = settings.resume_min_chars if min_chars is None else min_chars
    file_path = Path(path)

    if not file_path.is_file():
        return ParsedResume(method="failed", warnings=["file_missing"])

    suffix = file_path.suffix.lower()
    warnings: list[str] = []
    text = ""
    method = "failed"
    page_count = 0

    try:
        if suffix == ".pdf":
            text, page_count = _parse_pdf(file_path)
            method = "pdfplumber"
        elif suffix == ".docx":
            text = _parse_docx(file_path)
            method = "docx"
        else:
            return ParsedResume(method="failed", warnings=["unsupported_extension"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume_parse_failed", error=str(exc)[:300], suffix=suffix)
        return ParsedResume(method="failed", warnings=["parse_error"])

    if len(text) < threshold:
        warnings.append("text_too_short")
        if suffix == ".pdf":
            ocr_text = _ocr_pdf(file_path)
            if ocr_text and len(ocr_text) >= threshold:
                text = ocr_text
                method = "ocr"
                warnings.append("ocr_used")
            elif ocr_text is not None:
                warnings.append("ocr_low_quality")
        if len(text) < threshold:
            return ParsedResume(
                text=text, method="failed", page_count=page_count, warnings=warnings
            )

    return ParsedResume(
        text=text, method=method, page_count=page_count, warnings=warnings
    )
