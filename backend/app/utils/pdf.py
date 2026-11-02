"""PDF rendering of the CEO report.

The PDF mirrors the workbook: a short per-role summary followed by approved
shortlist rows. It deliberately has no contact, age, gender or photo columns.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.utils.excel import CeoReportRow

_CELL = ParagraphStyle(
    "Cell",
    fontName="Helvetica",
    fontSize=7.5,
    leading=9.5,
    textColor=colors.black,
)
_HEADER = ParagraphStyle(
    "Header",
    parent=_CELL,
    fontName="Helvetica-Bold",
    textColor=colors.white,
)
_TITLE = ParagraphStyle(
    "ReportTitle",
    fontName="Helvetica-Bold",
    fontSize=15,
    leading=19,
    spaceAfter=8,
)
_NOTE = ParagraphStyle(
    "Note",
    parent=_CELL,
    fontSize=8,
    leading=11,
    textColor=colors.HexColor("#444444"),
)


def _table(data: list[list], widths: list[float]) -> Table:
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B0B0B0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5FA")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def build_ceo_report_pdf(
    path: Path | str,
    *,
    title: str,
    summary_rows: list[dict],
    rows: list[CeoReportRow],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    page_width, _page_height = landscape(A4)
    usable = page_width - 30 * mm
    summary_widths = [usable * 0.4, usable * 0.15, usable * 0.15, usable * 0.15, usable * 0.15]
    row_widths = [
        usable * 0.18,
        usable * 0.15,
        usable * 0.07,
        usable * 0.2,
        usable * 0.2,
        usable * 0.15,
        usable * 0.05,
    ]

    story: list = [Paragraph(title, _TITLE)]
    story.append(
        Paragraph(
            "Approved shortlist only. Contact details, age, gender and photos are excluded.",
            _NOTE,
        )
    )
    story.append(Spacer(1, 6))

    if summary_rows:
        summary_data = [
            [Paragraph("Job", _HEADER)]
            + [
                Paragraph(header, _HEADER)
                for header in ("Applications", "Shortlisted", "Accepted", "Rejected")
            ]
        ]
        for summary in summary_rows:
            summary_data.append(
                [
                    Paragraph(str(summary.get("job", "")), _CELL),
                    Paragraph(str(summary.get("applications", 0)), _CELL),
                    Paragraph(str(summary.get("shortlisted", 0)), _CELL),
                    Paragraph(str(summary.get("accepted", 0)), _CELL),
                    Paragraph(str(summary.get("rejected", 0)), _CELL),
                ]
            )
        story.append(_table(summary_data, summary_widths))
        story.append(Spacer(1, 12))

    headers = [
        "Candidate",
        "Role",
        "Score",
        "Matched skills",
        "Missing skills",
        "Summary",
        "Decision",
    ]
    data: list[list] = [[Paragraph(header, _HEADER) for header in headers]]
    for row in rows:
        data.append(
            [
                Paragraph(row.candidate_name or "", _CELL),
                Paragraph(row.job_title or "", _CELL),
                Paragraph("" if row.score is None else str(row.score), _CELL),
                Paragraph(", ".join(row.matched_skills), _CELL),
                Paragraph(", ".join(row.missing_skills), _CELL),
                Paragraph(row.summary or "", _CELL),
                Paragraph(row.decision or "", _CELL),
            ]
        )
    if len(data) == 1:
        data.append([Paragraph("No approved candidates", _CELL)] + [""] * (len(headers) - 1))
    story.append(_table(data, row_widths))

    document = SimpleDocTemplate(
        str(target),
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=title,
        author="HR AI Agent System",
    )
    document.build(story)
    return target
