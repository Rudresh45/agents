from __future__ import annotations

from pathlib import Path

from app.utils.resume_parser import parse_resume
from docx import Document
from reportlab.pdfgen import canvas


def _make_docx(path: Path) -> Path:
    document = Document()
    document.add_paragraph("Priya Sharma")
    document.add_paragraph("Backend engineer with five years of Python and FastAPI experience.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Skills"
    table.cell(0, 1).text = "Python, PostgreSQL, Docker"
    document.save(path)
    return path


def _make_pdf(path: Path, text: str) -> Path:
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, text)
    pdf.save()
    return path


def test_parses_docx(tmp_path: Path):
    parsed = parse_resume(_make_docx(tmp_path / "resume.docx"), min_chars=10)
    assert parsed.method == "docx"
    assert "Priya Sharma" in parsed.text
    assert "PostgreSQL" in parsed.text


def test_parses_pdf(tmp_path: Path):
    path = _make_pdf(tmp_path / "resume.pdf", "Python developer with SQL experience")
    parsed = parse_resume(path, min_chars=10)
    assert parsed.method == "pdfplumber"
    assert "Python developer" in parsed.text
    assert parsed.page_count == 1


def test_missing_file_returns_failed(tmp_path: Path):
    parsed = parse_resume(tmp_path / "nope.pdf", min_chars=10)
    assert parsed.method == "failed"
    assert "file_missing" in parsed.warnings


def test_unsupported_extension_returns_failed(tmp_path: Path):
    path = tmp_path / "resume.txt"
    path.write_text("hello", encoding="utf-8")
    parsed = parse_resume(path, min_chars=10)
    assert parsed.method == "failed"
    assert "unsupported_extension" in parsed.warnings


def test_blank_pdf_falls_back_or_fails_cleanly(tmp_path: Path):
    path = tmp_path / "blank.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.showPage()
    pdf.save()
    parsed = parse_resume(path, min_chars=50)
    assert parsed.method in {"failed", "ocr"}
    if parsed.method == "failed":
        assert "text_too_short" in parsed.warnings


def test_corrupt_pdf_returns_failed(tmp_path: Path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.4 this is not a real pdf")
    parsed = parse_resume(path, min_chars=10)
    assert parsed.method == "failed"
    assert "parse_error" in parsed.warnings
