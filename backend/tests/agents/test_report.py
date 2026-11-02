from __future__ import annotations

from app.agents.ceo_report_agent.report import generate_report
from app.core.constants import CandidateStatus, DraftType
from app.db.models.ceo_report import CeoReport
from app.db.models.email_log import EmailLog
from openpyxl import load_workbook
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate
from tests.fakes import FakeMailer


def test_report_contains_only_accepted_candidates(ctx, session: Session, job, run):
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.ACCEPTED,
        score=88,
        full_name="Asha Rao",
        summary="Strong backend engineer.",
        matched_skills=["python"],
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.REJECTED,
        email="ravi@example.com",
        full_name="Ravi Kumar",
        score=20,
    )

    result = generate_report(ctx)

    assert result.counts["candidates"] == 1
    report = session.query(CeoReport).one()
    assert report.status == "sent"
    assert report.candidate_count == 1

    path = ctx.storage.root / "reports" / run.id / "ceo_report.xlsx"
    assert path.is_file()
    workbook = load_workbook(path)
    sheet = workbook["Shortlist"]
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert "Asha Rao" in values
    assert "Ravi Kumar" not in values

    headers = {cell.value for row in sheet.iter_rows() for cell in row if cell.value}
    for forbidden in ("Email", "Phone", "Gender", "Age", "Date of birth"):
        assert forbidden not in headers

    email, send = ctx.mailer.outbox[0]
    assert email.to == ["ceo@example.com"]
    assert send.dry_run is True
    log = session.query(EmailLog).one()
    assert log.status == "dry_run"


def test_report_blocked_while_approved_draft_is_unsent(
    ctx, session: Session, job, actor, run
):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    draft = ctx.services.drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Body",
        actor=actor,
        run_id=run.id,
    )
    ctx.services.drafts.approve(draft.id, actor=actor)

    result = generate_report(ctx)

    assert result.stop is True
    assert result.errors
    assert session.query(CeoReport).count() == 0
    assert ctx.mailer.outbox == []


def test_no_activity_means_no_report(ctx, session: Session, job):
    result = generate_report(ctx)

    assert result.counts == {"candidates": 0, "emailed": 0}
    assert session.query(CeoReport).count() == 0
    assert ctx.mailer.outbox == []


def test_smtp_failure_marks_report_failed(ctx, session: Session, settings, job):
    make_candidate(
        session, job_id=job.id, status=CandidateStatus.ACCEPTED, score=80
    )

    class BrokenMailer(FakeMailer):
        def send(self, email):  # noqa: ANN001
            raise RuntimeError("smtp down")

    ctx.mailer = BrokenMailer(settings)

    result = generate_report(ctx)

    assert result.errors
    report = session.query(CeoReport).one()
    assert report.status == "failed"
    log = session.query(EmailLog).one()
    assert log.status == "failed"


def test_summary_counts_come_from_candidate_statuses(ctx, session: Session, job):
    make_candidate(
        session, job_id=job.id, status=CandidateStatus.ACCEPTED, score=80
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.REJECTED,
        email="ravi@example.com",
        score=20,
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.NEEDS_REVIEW,
        email="neha@example.com",
    )

    generate_report(ctx)

    workbook = load_workbook(ctx.storage.root / "reports" / ctx.run_id / "ceo_report.xlsx")
    sheet = workbook["Shortlist"]
    row = [cell.value for cell in sheet[4]]
    assert row[0] == "Backend Engineer"
    assert row[1] == 3
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == 1
