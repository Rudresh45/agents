from __future__ import annotations

import pytest
from app.core.constants import CandidateStatus, ReplyCategory
from app.core.exceptions import ValidationFailedError
from app.db.models.candidate import Candidate
from app.schemas.domain import Actor
from app.services.reply_service import ReplyService
from sqlalchemy.orm import Session


def test_record_reply(session: Session, candidate: Candidate, actor: Actor):
    reply = ReplyService(session).record(
        candidate_id=candidate.id,
        category=ReplyCategory.QUESTION,
        actor=Actor.agent("hr_agent"),
        confidence=0.9,
        body_excerpt="When is the interview?",
    )
    assert reply.handled is False
    assert reply.category == "question"


def test_unknown_category_is_rejected(session: Session, candidate: Candidate, actor: Actor):
    with pytest.raises(ValidationFailedError):
        ReplyService(session).record(
            candidate_id=candidate.id,
            category="maybe",
            actor=Actor.agent("hr_agent"),
        )


def test_withdrawal_updates_candidate_and_flags_deletion(
    session: Session, candidate: Candidate, actor: Actor
):
    ReplyService(session).record(
        candidate_id=candidate.id,
        category=ReplyCategory.WITHDRAWN,
        actor=Actor.agent("hr_agent"),
        body_excerpt="I accepted another offer.",
    )
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.WITHDRAWN
    assert candidate.deletion_requested is True


def test_mark_handled(session: Session, candidate: Candidate, actor: Actor):
    service = ReplyService(session)
    reply = service.record(
        candidate_id=candidate.id,
        category=ReplyCategory.RESCHEDULE,
        actor=Actor.agent("hr_agent"),
    )
    handled = service.mark_handled(reply.id, actor=actor)
    assert handled.handled is True
    assert handled.handled_at is not None


def test_list_filters_by_handled(session: Session, candidate: Candidate, actor: Actor):
    service = ReplyService(session)
    service.record(
        candidate_id=candidate.id,
        category=ReplyCategory.OTHER,
        actor=Actor.agent("hr_agent"),
    )
    rows, total = service.list(handled=False)
    assert total == 1
    assert len(rows) == 1
