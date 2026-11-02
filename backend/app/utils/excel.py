"""Excel builders. Plain data in, workbook out; no knowledge of the database."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(bold=True, size=14)
WRAP = Alignment(wrap_text=True, vertical="top")


@dataclass
class ShortlistRow:
    candidate_name: str
    score: int | None
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    reason: str = ""
    decision: str = ""


@dataclass
class CeoReportRow:
    candidate_name: str
    job_title: str
    score: int | None
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    summary: str = ""
    decision: str = ""


def _style_header(worksheet, row: int, headers: list[str]) -> None:
    for column, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=row, column=column, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = WRAP
    worksheet.freeze_panes = worksheet.cell(row=row + 1, column=1)


def _widths(worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def build_shortlist(path: Path | str, jobs: dict[str, list[ShortlistRow]]) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)

    for job_title, rows in jobs.items():
        sheet = workbook.create_sheet(title=(job_title or "Candidates")[:31])
        sheet["A1"] = job_title
        sheet["A1"].font = TITLE_FONT
        headers = [
            "Candidate",
            "Score",
            "Matched skills",
            "Missing skills",
            "Reason",
            "Decision",
        ]
        _style_header(sheet, 3, headers)
        for offset, row in enumerate(rows, start=4):
            sheet.cell(row=offset, column=1, value=row.candidate_name)
            sheet.cell(row=offset, column=2, value=row.score)
            sheet.cell(row=offset, column=3, value=", ".join(row.matched_skills))
            sheet.cell(row=offset, column=4, value=", ".join(row.missing_skills))
            sheet.cell(row=offset, column=5, value=row.reason).alignment = WRAP
            sheet.cell(row=offset, column=6, value=row.decision)
        _widths(sheet, [26, 8, 34, 34, 60, 16])

    if not workbook.worksheets:
        workbook.create_sheet(title="No candidates")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    return target


def build_ceo_report(
    path: Path | str,
    *,
    title: str,
    summary_rows: list[dict],
    rows: list[CeoReportRow],
) -> Path:
    """Summary block plus one row per approved shortlisted candidate.

    Deliberately no contact, age, gender or photo columns (spec section 9.7).
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Shortlist"
    sheet["A1"] = title
    sheet["A1"].font = TITLE_FONT

    summary_headers = ["Job", "Applications", "Shortlisted", "Accepted", "Rejected"]
    _style_header(sheet, 3, summary_headers)
    for offset, summary in enumerate(summary_rows, start=4):
        sheet.cell(row=offset, column=1, value=summary.get("job", ""))
        sheet.cell(row=offset, column=2, value=summary.get("applications", 0))
        sheet.cell(row=offset, column=3, value=summary.get("shortlisted", 0))
        sheet.cell(row=offset, column=4, value=summary.get("accepted", 0))
        sheet.cell(row=offset, column=5, value=summary.get("rejected", 0))

    start = 4 + len(summary_rows) + 1
    headers = [
        "Candidate",
        "Role",
        "Score",
        "Matched skills",
        "Missing skills",
        "Summary",
        "Decision",
    ]
    _style_header(sheet, start, headers)
    for offset, row in enumerate(rows, start=start + 1):
        sheet.cell(row=offset, column=1, value=row.candidate_name)
        sheet.cell(row=offset, column=2, value=row.job_title)
        sheet.cell(row=offset, column=3, value=row.score)
        sheet.cell(row=offset, column=4, value=", ".join(row.matched_skills))
        sheet.cell(row=offset, column=5, value=", ".join(row.missing_skills))
        sheet.cell(row=offset, column=6, value=row.summary).alignment = WRAP
        sheet.cell(row=offset, column=7, value=row.decision)
    _widths(sheet, [26, 24, 8, 34, 34, 60, 16])

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    return target
