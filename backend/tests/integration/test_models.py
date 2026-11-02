from __future__ import annotations

import pytest
from app.core.constants import CandidateStatus, DraftType, RunStatus
from app.core.exceptions import AuditImmutableError
from app.db.base import utcnow
from app.db.models import (
    AuditLog,
    Candidate,
    EmailDraft,
    Job,
    User,
    WorkflowRun,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _candidate(session: Session, email: str = "a@example.com", **kwargs) -> Candidate:
    candidate = Candidate(full_name="A Candidate", email=email, **kwargs)
    session.add(candidate)
    session.commit()
    return candidate


def test_user_and_job_roundtrip(session: Session):
    user = User(email="hr@example.com", password_hash="x", role="hr", full_name="HR")
    session.add(user)
    session.commit()

    job = Job(
        title="Backend Engineer",
        required_skills=["python", "sql"],
        preferred_skills=["fastapi"],
        min_experience_years=2,
        min_score=65,
        created_by_id=user.id,
    )
    session.add(job)
    session.commit()

    assert session.get(Job, job.id).required_skills == ["python", "sql"]
    assert session.get(Job, job.id).created_by_id == user.id


def test_duplicate_candidate_same_job_is_rejected(session: Session):
    job = Job(title="Engineer", required_skills=["python"])
    session.add(job)
    session.commit()

    _candidate(session, "dup@example.com", job_id=job.id)
    session.add(Candidate(full_name="Dup", email="dup@example.com", job_id=job.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_same_email_different_job_is_allowed(session: Session):
    job_a = Job(title="A", required_skills=["python"])
    job_b = Job(title="B", required_skills=["sql"])
    session.add_all([job_a, job_b])
    session.commit()

    _candidate(session, "same@example.com", job_id=job_a.id)
    _candidate(session, "same@example.com", job_id=job_b.id)
    assert session.query(Candidate).count() == 2


def test_one_active_draft_per_candidate_and_type(session: Session):
    candidate = _candidate(session)
    first = EmailDraft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="s",
        body="b",
    )
    session.add(first)
    session.commit()

    session.add(
        EmailDraft(
            candidate_id=candidate.id,
            draft_type=DraftType.INTERVIEW_INVITE,
            subject="s2",
            body="b2",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Superseding the first draft frees the slot.
    first.superseded_at = utcnow()
    session.commit()
    session.add(
        EmailDraft(
            candidate_id=candidate.id,
            draft_type=DraftType.INTERVIEW_INVITE,
            subject="s3",
            body="b3",
            version=2,
        )
    )
    session.commit()
    assert session.query(EmailDraft).count() == 2


def test_only_one_active_workflow_run(session: Session):
    first = WorkflowRun(status=RunStatus.RUNNING, active_slot=1, dry_run=True)
    session.add(first)
    session.commit()

    session.add(WorkflowRun(status=RunStatus.RUNNING, active_slot=1, dry_run=True))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    first.status = RunStatus.FINISHED
    first.active_slot = None
    first.finished_at = utcnow()
    session.commit()

    session.add(WorkflowRun(status=RunStatus.RUNNING, active_slot=1, dry_run=True))
    session.commit()
    assert session.query(WorkflowRun).count() == 2


def test_audit_log_is_append_only(session: Session):
    entry = AuditLog(
        actor_type="agent",
        actor_id="hr_agent",
        action="candidate.status_changed",
        entity_type="candidate",
        entity_id="c1",
        old_value={"status": CandidateStatus.RECEIVED},
        new_value={"status": CandidateStatus.FORWARDED},
    )
    session.add(entry)
    session.commit()

    entry.reason = "edited"
    with pytest.raises(AuditImmutableError):
        session.commit()
    session.rollback()

    stored = session.query(AuditLog).one()
    with pytest.raises(AuditImmutableError):
        session.delete(stored)
        session.commit()
    session.rollback()

    assert session.query(AuditLog).count() == 1


def test_candidate_defaults(session: Session):
    candidate = _candidate(session)
    assert candidate.status == CandidateStatus.RECEIVED
    assert candidate.matched_skills == []
    assert candidate.missing_skills == []
    assert candidate.consent_given is False
    assert candidate.anonymised is False
