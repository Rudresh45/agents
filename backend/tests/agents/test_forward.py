from __future__ import annotations

import pytest
from app.agents.hr_agent.forward import forward_candidates
from app.core.constants import CandidateStatus
from app.db.models.candidate import Candidate
from app.db.models.email_log import EmailLog
from app.schemas.llm import CandidateSummary
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate
from tests.fakes import FakeMailer


def _attach_resume(ctx, session: Session, candidate) -> None:
    stored = ctx.storage.save(b"%PDF-1.4 resume body", "resume.pdf", candidate.id)
    candidate.resume_path = stored.relative_path
    session.commit()


def test_forwards_received_candidates(ctx, session: Session, job):
    first = make_candidate(session, job_id=job.id, email="asha@example.com")
    second = make_candidate(
        session,
        job_id=job.id,
        email="ravi@example.com",
        full_name="Ravi Kumar",
    )
    _attach_resume(ctx, session, first)
    ctx.llm.responses = {
        "CandidateSummary": CandidateSummary(summary="Strong Python and SQL background.")
    }

    result = forward_candidates(ctx)

    assert result.counts["forwarded"] == 2
    assert result.errors == []
    for candidate in (first, second):
        session.refresh(candidate)
        assert candidate.status == CandidateStatus.FORWARDED
        assert candidate.summary == "Strong Python and SQL background."

    email, send = ctx.mailer.outbox[0]
    assert email.to == ["manager@example.com"]
    assert "Candidate digest" in email.subject
    assert "Asha Rao" in email.body_text
    assert len(email.attachments) == 1
    assert send.dry_run is True

    log = session.query(EmailLog).one()
    assert log.status == "dry_run"
    assert log.to_address == "manager@example.com"


def test_no_candidates_means_no_digest(ctx, session: Session, job):
    result = forward_candidates(ctx)
    assert result.counts["forwarded"] == 0
    assert ctx.mailer.outbox == []


def test_candidate_without_resume_text_is_sent_back(ctx, session: Session, job):
    candidate = make_candidate(session, job_id=job.id, resume_text="")

    result = forward_candidates(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.NEEDS_RESUBMISSION
    assert result.counts["forwarded"] == 0
    assert ctx.mailer.outbox == []


def test_unassigned_candidate_is_skipped(ctx, session: Session, job):
    candidate = make_candidate(session, job_id=None)

    result = forward_candidates(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.RECEIVED
    assert result.counts["skipped"] == 1
    assert ctx.mailer.outbox == []


def test_smtp_failure_leaves_candidates_received(ctx, session: Session, settings, job):
    candidate = make_candidate(session, job_id=job.id)

    class BrokenMailer(FakeMailer):
        def send(self, email):  # noqa: ANN001
            raise RuntimeError("smtp down")

    ctx.mailer = BrokenMailer(settings)

    result = forward_candidates(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.RECEIVED
    assert result.errors
    log = session.query(EmailLog).one()
    assert log.status == "failed"


def test_llm_summary_failure_still_forwards(ctx, session: Session, job):
    candidate = make_candidate(session, job_id=job.id)
    ctx.llm.fail_times = 1

    result = forward_candidates(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FORWARDED
    assert candidate.summary == "Summary unavailable."
    assert result.errors[0]["node"] == "forward_to_manager"
    assert len(ctx.mailer.outbox) == 1


def test_digest_is_not_sent_twice(ctx, session: Session, job):
    make_candidate(session, job_id=job.id)
    ctx.llm.responses = {"CandidateSummary": CandidateSummary(summary="Good.")}

    forward_candidates(ctx)
    second = forward_candidates(ctx)

    assert len(ctx.mailer.outbox) == 1
    assert second.counts["forwarded"] == 0


@pytest.mark.parametrize("status", [CandidateStatus.SHORTLISTED])
def test_non_received_candidates_are_ignored(ctx, session: Session, job, status):
    make_candidate(session, job_id=job.id, status=status, score=70)
    result = forward_candidates(ctx)
    assert result.counts["forwarded"] == 0
    assert session.query(Candidate).one().status == status
