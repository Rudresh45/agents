from __future__ import annotations

from app.agents.hr_agent.send import send_approved
from app.core.constants import CandidateStatus, DraftType
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate
from tests.fakes import FakeMailer


def _approved_draft(ctx, candidate, draft_type: str, run_id: str, actor):
    draft = ctx.services.drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=draft_type,
        subject="Your application",
        body="Dear Candidate,\n\nUpdate on your application.",
        actor=actor,
        run_id=run_id,
    )
    ctx.services.drafts.approve(draft.id, actor=actor)
    return ctx.services.drafts.get(draft.id)


def test_approved_invite_is_sent_and_candidate_accepted(
    ctx, session: Session, job, actor, run
):
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
    )
    draft = _approved_draft(ctx, candidate, DraftType.INTERVIEW_INVITE, run.id, actor)

    result = send_approved(ctx)

    assert result.counts["sent"] == 1
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.ACCEPTED
    assert ctx.services.drafts.get(draft.id).sent_at is not None
    email, send = ctx.mailer.outbox[0]
    assert email.to == ["asha@example.com"]
    assert send.dry_run is True
    log = session.query(EmailLog).one()
    assert log.status == "dry_run"
    assert log.draft_id == draft.id


def test_unapproved_draft_is_never_sent(ctx, session: Session, job, actor, run):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    ctx.services.drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Body",
        actor=actor,
        run_id=run.id,
    )

    result = send_approved(ctx)

    assert result.counts["sent"] == 0
    assert ctx.mailer.outbox == []
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.SHORTLISTED


def test_rejection_sets_rejected(ctx, session: Session, job, actor, run):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.NOT_SHORTLISTED, score=25
    )
    _approved_draft(ctx, candidate, DraftType.REJECTION, run.id, actor)

    send_approved(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.REJECTED
    assert len(ctx.mailer.outbox) == 1


def test_draft_from_another_run_is_not_sent(ctx, session: Session, job, actor, run):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    ctx.services.drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Old run",
        body="Body",
        actor=actor,
        run_id=None,
    )
    draft = session.query(EmailDraft).one()
    ctx.services.drafts.approve(draft.id, actor=actor)

    result = send_approved(ctx)

    assert result.counts["sent"] == 0
    assert ctx.mailer.outbox == []


def test_smtp_failure_marks_draft_and_leaves_status(
    ctx, session: Session, settings, job, actor, run
):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    draft = _approved_draft(ctx, candidate, DraftType.INTERVIEW_INVITE, run.id, actor)

    class BrokenMailer(FakeMailer):
        def send(self, email):  # noqa: ANN001
            raise RuntimeError("smtp down")

    ctx.mailer = BrokenMailer(settings)

    result = send_approved(ctx)

    assert result.counts["sent"] == 0
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.SHORTLISTED
    assert ctx.services.drafts.get(draft.id).send_error == "smtp down"
    log = session.query(EmailLog).one()
    assert log.status == "failed"
