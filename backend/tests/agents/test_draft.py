from __future__ import annotations

from app.agents.hr_agent.draft import draft_decisions
from app.core.constants import CandidateStatus, DraftType
from app.db.models.email_draft import EmailDraft
from app.schemas.llm import DraftEmail
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate


def test_creates_interview_invite_draft(ctx, session: Session, job, actor, run):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=82
    )
    ctx.llm.responses = {
        "DraftEmail": DraftEmail(
            subject="Interview invitation - Backend Engineer",
            body="Dear Candidate,\n\nWe would like to invite you for an interview.",
        )
    }

    result = draft_decisions(ctx)

    assert result.counts["drafts"] == 1
    draft = session.query(EmailDraft).one()
    assert draft.candidate_id == candidate.id
    assert draft.draft_type == DraftType.INTERVIEW_INVITE
    assert draft.approved is False
    assert draft.run_id == run.id
    assert draft.model_name == "gemini-test"
    assert draft.prompt_version


def test_creates_rejection_draft(ctx, session: Session, job):
    make_candidate(
        session, job_id=job.id, status=CandidateStatus.NOT_SHORTLISTED, score=30
    )
    ctx.llm.responses = {
        "DraftEmail": DraftEmail(subject="Update on your application", body="Dear Candidate...")
    }

    draft_decisions(ctx)

    draft = session.query(EmailDraft).one()
    assert draft.draft_type == DraftType.REJECTION


def test_candidate_name_is_not_sent_to_the_llm(ctx, session: Session, job):
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
        full_name="Asha Rao",
        summary="Strong backend engineer.",
    )
    ctx.llm.responses = {"DraftEmail": DraftEmail(subject="Interview", body="Hello")}

    draft_decisions(ctx)

    assert all("Asha Rao" not in call["user"] for call in ctx.llm.calls)


def test_existing_active_draft_is_not_replaced(ctx, session: Session, job, actor, run):
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    ctx.services.drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Existing",
        body="Existing body",
        actor=actor,
        run_id=run.id,
    )

    result = draft_decisions(ctx)

    assert result.counts["skipped"] == 1
    assert ctx.llm.calls == []
    assert session.query(EmailDraft).count() == 1


def test_llm_failure_is_recorded(ctx, session: Session, job):
    make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )
    ctx.llm.fail_times = 1

    result = draft_decisions(ctx)

    assert result.counts["drafts"] == 0
    assert result.errors[0]["node"] == "draft_decisions"
    assert session.query(EmailDraft).count() == 0


def test_closed_jobs_get_no_drafts(ctx, session: Session, job):
    job.status = "closed"
    session.commit()
    make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=80
    )

    result = draft_decisions(ctx)

    assert result.counts["drafts"] == 0
    assert session.query(EmailDraft).count() == 0


def test_only_shortlisted_and_rejected_candidates_are_drafted(ctx, session: Session, job):
    make_candidate(session, job_id=job.id, status=CandidateStatus.FORWARDED)
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.ACCEPTED,
        email="accepted@example.com",
        score=90,
    )

    result = draft_decisions(ctx)

    assert result.counts["drafts"] == 0
    assert ctx.llm.calls == []
