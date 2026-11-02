from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from app.core.constants import CandidateStatus, DraftType
from app.core.exceptions import (
    ApprovalRequiredError,
    DuplicateCandidateError,
    InvalidTransitionError,
    ReasonRequiredError,
    ValidationFailedError,
)
from app.db.models.audit_log import AuditLog
from app.db.models.candidate import Candidate
from app.db.models.job import Job
from app.schemas.domain import Actor, ApplicationInput
from app.services.audit_service import AuditService
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService
from sqlalchemy.orm import Session


def _application(**overrides) -> ApplicationInput:
    data = {
        "full_name": "New Person",
        "email": "new@example.com",
        "phone": "+91 90000 00000",
        "job_id": None,
    }
    data.update(overrides)
    return ApplicationInput(**data)


def test_create_sets_status_retention_and_audit(session: Session, actor: Actor, job: Job):
    service = CandidateService(session)
    created = service.create_from_application(
        _application(job_id=job.id, consent_given=True, consent_source="email"), actor=actor
    )
    assert created.status == CandidateStatus.RECEIVED
    assert created.retention_until == date.today() + timedelta(days=180)
    assert created.consent_given is True
    audit = session.query(AuditLog).filter(AuditLog.action == "candidate.created").one()
    assert audit.actor_id == actor.id


def test_duplicate_same_email_and_job_is_rejected(session: Session, actor: Actor, job: Job):
    service = CandidateService(session)
    service.create_from_application(_application(job_id=job.id), actor=actor)
    with pytest.raises(DuplicateCandidateError):
        service.create_from_application(_application(job_id=job.id), actor=actor)


def test_same_email_different_job_is_allowed(session: Session, actor: Actor, job: Job):
    other = Job(title="Analyst", required_skills=["sql"])
    session.add(other)
    session.commit()

    service = CandidateService(session)
    service.create_from_application(_application(job_id=job.id), actor=actor)
    created = service.create_from_application(_application(job_id=other.id), actor=actor)
    assert created.job_id == other.id


def test_needs_review_duplicates_are_detected(session: Session, actor: Actor):
    service = CandidateService(session)
    service.create_from_application(_application(), actor=actor)
    with pytest.raises(DuplicateCandidateError):
        service.create_from_application(_application(), actor=actor)


def test_assign_job_moves_needs_review_to_received(
    session: Session, actor: Actor, job: Job
):
    service = CandidateService(session)
    created = service.create_from_application(_application(), actor=actor)
    service.transition(created.id, CandidateStatus.NEEDS_REVIEW, actor=actor)
    updated = service.assign_job(created.id, job.id, actor=actor)
    assert updated.job_id == job.id
    assert updated.status == CandidateStatus.RECEIVED


def test_assign_closed_job_is_rejected(session: Session, actor: Actor, job: Job):
    service = CandidateService(session)
    created = service.create_from_application(_application(), actor=actor)
    job.status = "closed"
    session.commit()
    with pytest.raises(ValidationFailedError):
        service.assign_job(created.id, job.id, actor=actor)


def test_invalid_transition_is_rejected(session: Session, actor: Actor, candidate: Candidate):
    service = CandidateService(session)
    with pytest.raises(InvalidTransitionError):
        service.transition(candidate.id, CandidateStatus.ACCEPTED, actor=actor)


def test_override_requires_reason(session: Session, actor: Actor, candidate: Candidate):
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    service.transition(candidate.id, CandidateStatus.SHORTLISTED, actor=actor)
    with pytest.raises(ReasonRequiredError):
        service.transition(
            candidate.id, CandidateStatus.NOT_SHORTLISTED, actor=actor, reason=""
        )


def test_override_with_reason_is_allowed(session: Session, actor: Actor, candidate: Candidate):
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    service.transition(candidate.id, CandidateStatus.SHORTLISTED, actor=actor)
    updated = service.transition(
        candidate.id,
        CandidateStatus.NOT_SHORTLISTED,
        actor=actor,
        reason="Missing core SQL depth",
    )
    assert updated.status == CandidateStatus.NOT_SHORTLISTED


def test_accepted_requires_an_approved_matching_draft(
    session: Session, actor: Actor, candidate: Candidate
):
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    service.transition(candidate.id, CandidateStatus.SHORTLISTED, actor=actor)

    with pytest.raises(ApprovalRequiredError):
        service.transition(candidate.id, CandidateStatus.ACCEPTED, actor=actor)

    drafts = DraftService(session)
    draft = drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Come in",
        actor=Actor.agent("hr_agent"),
    )
    drafts.approve(draft.id, actor=actor)
    updated = service.transition(candidate.id, CandidateStatus.ACCEPTED, actor=actor)
    assert updated.status == CandidateStatus.ACCEPTED


def test_rejected_requires_a_rejection_draft_not_an_invite(
    session: Session, actor: Actor, candidate: Candidate
):
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    service.transition(candidate.id, CandidateStatus.NOT_SHORTLISTED, actor=actor)

    drafts = DraftService(session)
    invite = drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Come in",
        actor=Actor.agent("hr_agent"),
    )
    drafts.approve(invite.id, actor=actor)
    with pytest.raises(ApprovalRequiredError):
        service.transition(candidate.id, CandidateStatus.REJECTED, actor=actor)


def test_every_status_change_writes_audit(
    session: Session, actor: Actor, candidate: Candidate
):
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    entries = (
        session.query(AuditLog)
        .filter(AuditLog.action == "candidate.status_changed")
        .all()
    )
    assert len(entries) == 1
    assert entries[0].old_value == {"status": "received"}
    assert entries[0].new_value == {"status": "forwarded"}


def test_scoring_requires_a_job(session: Session, actor: Actor):
    service = CandidateService(session)
    created = service.create_from_application(_application(), actor=actor)
    with pytest.raises(ValidationFailedError):
        service.save_score(
            created.id,
            score=80,
            reason="good",
            matched_skills=["python"],
            missing_skills=[],
            model_name="gemini",
            prompt_version="screening.v1",
        )


def test_save_score_persists_model_and_prompt(
    session: Session, actor: Actor, candidate: Candidate
):
    service = CandidateService(session)
    updated = service.save_score(
        candidate.id,
        score=82,
        reason="Strong Python.",
        matched_skills=["python"],
        missing_skills=["sql"],
        model_name="gemini-1.5-flash",
        prompt_version="screening.v1",
    )
    assert updated.score == 82
    assert updated.prompt_version == "screening.v1"
    assert updated.scored_at is not None
    audit = session.query(AuditLog).filter(AuditLog.action == "candidate.scored").one()
    assert audit.model_name == "gemini-1.5-flash"
    assert audit.prompt_version == "screening.v1"


def test_withdraw_requires_reason_and_flags_deletion(
    session: Session, actor: Actor, candidate: Candidate
):
    service = CandidateService(session)
    with pytest.raises(ReasonRequiredError):
        service.withdraw(candidate.id, actor=actor, reason="")
    updated = service.withdraw(
        candidate.id, actor=actor, reason="Accepted another offer"
    )
    assert updated.status == CandidateStatus.WITHDRAWN
    assert updated.deletion_requested is True


def test_anonymise_removes_pii_and_file(
    session: Session, actor: Actor, candidate: Candidate, tmp_path: Path
):
    from app.utils.storage import Storage

    storage = Storage()
    stored = storage.save(b"%PDF-1.4 test", "cv.pdf", candidate.id)
    candidate.resume_path = stored.relative_path
    session.commit()

    CandidateService(session).anonymise(candidate.id, actor=actor)
    session.refresh(candidate)
    assert candidate.anonymised is True
    assert candidate.full_name == "Anonymised"
    assert "candidate@example.com" not in candidate.email
    assert candidate.resume_path is None
    assert not (Path(storage.root) / stored.relative_path).exists()


def test_hard_delete_removes_candidate_and_drafts(
    session: Session, actor: Actor, candidate: Candidate
):
    drafts = DraftService(session)
    drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.REJECTION,
        subject="No",
        body="No thanks",
        actor=actor,
    )
    CandidateService(session).hard_delete(candidate.id, actor=actor)
    assert session.get(Candidate, candidate.id) is None
    audit = session.query(AuditLog).filter(AuditLog.action == "privacy.deleted").one()
    assert audit.entity_id == candidate.id


def test_list_filters_and_pagination(session: Session, actor: Actor, job: Job):
    service = CandidateService(session)
    for index in range(5):
        created = service.create_from_application(
            _application(email=f"p{index}@example.com", job_id=job.id), actor=actor
        )
        service.save_score(
            created.id,
            score=50 + index,
            reason="r",
            matched_skills=[],
            missing_skills=[],
            model_name="m",
            prompt_version="p",
        )
    rows, total = service.list(job_id=job.id, min_score=52, page=1, page_size=2)
    assert total == 3
    assert len(rows) == 2
    assert all(row.score >= 52 for row in rows)


def test_counts_by_status(session: Session, actor: Actor, candidate: Candidate):
    counts = CandidateService(session).counts_by_status()
    assert counts["received"] == 1


def test_audit_export_streams_rows(session: Session, actor: Actor, candidate: Candidate):
    CandidateService(session).transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    rows = list(AuditService(session).export_rows(entity_type="candidate"))
    assert len(rows) == 1
    assert rows[0]["action"] == "candidate.status_changed"
