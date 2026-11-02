from __future__ import annotations

from pathlib import Path

from app.utils.excel import CeoReportRow, ShortlistRow, build_ceo_report, build_shortlist
from openpyxl import load_workbook


def test_shortlist_has_one_sheet_per_job(tmp_path: Path):
    path = build_shortlist(
        tmp_path / "shortlist.xlsx",
        {
            "Backend Engineer": [
                ShortlistRow(
                    candidate_name="A Candidate",
                    score=82,
                    matched_skills=["python", "sql"],
                    missing_skills=["docker"],
                    reason="Strong backend fit.",
                    decision="shortlisted",
                )
            ],
            "Data Analyst": [],
        },
    )
    workbook = load_workbook(path)
    assert workbook.sheetnames == ["Backend Engineer", "Data Analyst"]
    sheet = workbook["Backend Engineer"]
    headers = [sheet.cell(row=3, column=column).value for column in range(1, 7)]
    assert headers == [
        "Candidate",
        "Score",
        "Matched skills",
        "Missing skills",
        "Reason",
        "Decision",
    ]
    assert sheet["A4"].value == "A Candidate"
    assert sheet["B4"].value == 82


def test_ceo_report_excludes_contact_and_protected_fields(tmp_path: Path):
    path = build_ceo_report(
        tmp_path / "ceo.xlsx",
        title="Approved shortlist",
        summary_rows=[
            {
                "job": "Backend Engineer",
                "applications": 10,
                "shortlisted": 3,
                "accepted": 2,
                "rejected": 1,
            }
        ],
        rows=[
            CeoReportRow(
                candidate_name="A Candidate",
                job_title="Backend Engineer",
                score=82,
                matched_skills=["python"],
                missing_skills=["docker"],
                summary="Five years of backend work.",
                decision="accepted",
            )
        ],
    )
    workbook = load_workbook(path)
    sheet = workbook.active
    all_values = [
        str(cell.value)
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    ]
    joined = " ".join(all_values).lower()
    for forbidden in ["email", "phone", "gender", "age", "nationality", "photo"]:
        assert forbidden not in joined
    assert "A Candidate" in all_values
    assert "accepted" in joined
