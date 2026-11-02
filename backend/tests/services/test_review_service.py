from __future__ import annotations

import pytest
from app.core.constants import CandidateStatus, DraftType
from app.core.exceptions import ReasonRequiredError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.schemas.domain import Actor
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService
from app.services.review_service import ReviewService
from sqlalchemy.orm import Session


def _forward_and_shortlist(session: Session, candidate: Candidate, actor: Actor) -> None:
    service = CandidateService(session)
    service.transition(candidate.id, CandidateStatus.FORWARDED, actor=actor)
    service.transition(candidate.id, CandidateStatus.SHORTLISTED, actor=actor)


def test_override_requires_reason(session: Session, candidate: Candidate, actor: Actor):
    _forward_and_shortlist(session, candidate, actor)
    with pytest.raises(ReasonRequiredError):
        ReviewService(session).override_decision(
            candidate.id, decision=CandidateStatus.NOT_SHORTLISTED, reason="  ", actor=actor
        )


def test_override_changes_status_and_records_reason(
    session: Session, candidate: Candidate, actor: Actor
):
    _forward_and_shortlist(session, candidate, actor)
    updated, redraft = ReviewService(session).override_decision(
        candidate.id,
        decision=CandidateStatus.NOT_SHORTLISTED,
        reason="Not enough SQL depth",
        actor=actor,
    )
    assert updated.status == CandidateStatus.NOT_SHORTLISTED
    assert updated.decision_override_reason == "Not enough SQL depth"
    assert redraft is False


def test_override_supersedes_active_draft(session: Session, candidate: Candidate, actor: Actor):
    _forward_and_shortlist(session, candidate, actor)
    drafts = DraftService(session)
    draft = drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Come in",
        body="Interview",
        actor=actor,
    )
    drafts.approve(draft.id, actor=actor)

    _, redraft = ReviewService(session).override_decision(
        candidate.id,
        decision=CandidateStatus.NOT_SHORTLISTED,
        reason="Score overstated",
        actor=actor,
    )
    assert redraft is True
    assert drafts.get(draft.id).superseded_at is not None


def test_same_decision_is_idempotent(session: Session, candidate: Candidate, actor: Actor):
    _forward_and_shortlist(session, candidate, actor)
    updated, redraft = ReviewService(session).override_decision(
        candidate.id,
        decision=CandidateStatus.SHORTLISTED,
        reason="Confirmed by HR",
        actor=actor,
    )
    assert updated.status == CandidateStatus.SHORTLISTED
    assert redraft is False


def test_invalid_decision_is_rejected(session: Session, candidate: Candidate, actor: Actor):
    _forward_and_shortlist(session, candidate, actor)
    with pytest.raises(ValidationFailedError):
        ReviewService(session).override_decision(
            candidate.id, decision=CandidateStatus.ACCEPTED, reason="nope", actor=actor
        )
