from __future__ import annotations

from app.agents.hr_manager_agent.shortlist import build_shortlist_file
from app.core.constants import CandidateStatus
from app.db.models.email_log import EmailLog
from openpyxl import load_workbook
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate


def test_builds_and_emails_shortlist(ctx, session: Session, job, run):
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=84,
        full_name="Asha Rao",
        matched_skills=["python"],
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.NOT_SHORTLISTED,
        email="ravi@example.com",
        full_name="Ravi Kumar",
        score=32,
    )

    result = build_shortlist_file(ctx)

    assert result.counts["candidates"] == 2
    path = ctx.storage.root / "reports" / run.id / "shortlist.xlsx"
    assert path.is_file()
    workbook = load_workbook(path)
    assert "Backend Engineer" in workbook.sheetnames
    sheet = workbook["Backend Engineer"]
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert "Asha Rao" in values
    assert "Ravi Kumar" in values

    email, send = ctx.mailer.outbox[0]
    assert email.to == ["hr@example.com"]
    assert email.attachments == [str(path)]
    assert send.dry_run is True
    log = session.query(EmailLog).one()
    assert log.status == "dry_run"


def test_no_scored_candidates_means_no_email(ctx, session: Session, job):
    make_candidate(session, job_id=job.id, status=CandidateStatus.FORWARDED)

    result = build_shortlist_file(ctx)

    assert result.counts["candidates"] == 0
    assert ctx.mailer.outbox == []


def test_candidate_without_score_is_skipped(ctx, session: Session, job):
    make_candidate(session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=None)

    result = build_shortlist_file(ctx)

    assert result.counts["candidates"] == 0


def test_smtp_failure_is_recorded(ctx, session: Session, settings, job):
    from tests.fakes import FakeMailer

    make_candidate(session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=75)

    class BrokenMailer(FakeMailer):
        def send(self, email):  # noqa: ANN001
            raise RuntimeError("smtp down")

    ctx.mailer = BrokenMailer(settings)

    result = build_shortlist_file(ctx)

    assert result.errors
    log = session.query(EmailLog).one()
    assert log.status == "failed"
