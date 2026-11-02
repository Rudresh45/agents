from __future__ import annotations

import pytest
from app.core.constants import DraftType
from app.core.exceptions import (
    ApprovalRequiredError,
    NotFoundError,
    ValidationFailedError,
)
from app.db.models.audit_log import AuditLog
from app.db.models.candidate import Candidate
from app.schemas.domain import Actor
from app.services.draft_service import DraftService
from sqlalchemy.orm import Session


def _create(session: Session, candidate: Candidate, actor: Actor, **overrides):
    data = {
        "candidate_id": candidate.id,
        "draft_type": DraftType.INTERVIEW_INVITE,
        "subject": "Interview invitation",
        "body": "We would like to meet you.",
        "actor": actor,
    }
    data.update(overrides)
    return DraftService(session).create_draft(**data)


def test_create_draft_is_unapproved(session: Session, candidate: Candidate, actor: Actor):
    draft = _create(session, candidate, actor)
    assert draft.approved is False
    assert draft.version == 1
    assert draft.sent_at is None


def test_new_version_supersedes_previous(session: Session, candidate: Candidate, actor: Actor):
    first = _create(session, candidate, actor)
    second = _create(session, candidate, actor, subject="Updated subject")
    assert second.version == 2
    refreshed = DraftService(session).get(first.id)
    assert refreshed.superseded_at is not None
    assert refreshed.approved is False


def test_only_one_active_draft_per_type(session: Session, candidate: Candidate, actor: Actor):
    _create(session, candidate, actor)
    _create(session, candidate, actor, draft_type=DraftType.REJECTION)
    drafts, total = DraftService(session).list(candidate_id=candidate.id)
    assert total == 2


def test_unknown_draft_type_is_rejected(session: Session, candidate: Candidate, actor: Actor):
    with pytest.raises(ValidationFailedError):
        _create(session, candidate, actor, draft_type="love_letter")


def test_approve_records_approver(session: Session, candidate: Candidate, actor: Actor):
    draft = _create(session, candidate, actor)
    approved = DraftService(session).approve(draft.id, actor=actor)
    assert approved.approved is True
    assert approved.approved_by_id == actor.id
    assert approved.approved_at is not None


def test_revoke_clears_approval(session: Session, candidate: Candidate, actor: Actor):
    service = DraftService(session)
    draft = _create(session, candidate, actor)
    service.approve(draft.id, actor=actor)
    revoked = service.revoke_approval(draft.id, actor=actor)
    assert revoked.approved is False
    assert revoked.approved_by_id is None


def test_edit_updates_subject_and_body(session: Session, candidate: Candidate, actor: Actor):
    draft = _create(session, candidate, actor)
    updated = DraftService(session).update_draft(
        draft.id, subject="New subject", body="New body", actor=actor
    )
    assert updated.subject == "New subject"
    assert updated.body == "New body"


def test_sent_draft_cannot_be_edited_or_reapproved(
    session: Session, candidate: Candidate, actor: Actor
):
    service = DraftService(session)
    draft = _create(session, candidate, actor)
    service.approve(draft.id, actor=actor)
    service.mark_sent(draft.id, actor=actor)

    with pytest.raises(ValidationFailedError):
        service.update_draft(draft.id, subject="x", body=None, actor=actor)
    with pytest.raises(ValidationFailedError):
        service.approve(draft.id, actor=actor)


def test_assert_sendable_blocks_unapproved(session: Session, candidate: Candidate, actor: Actor):
    draft = _create(session, candidate, actor)
    with pytest.raises(ApprovalRequiredError):
        DraftService(session).assert_sendable(draft.id)


def test_assert_sendable_passes_for_approved(session: Session, candidate: Candidate, actor: Actor):
    service = DraftService(session)
    draft = _create(session, candidate, actor)
    service.approve(draft.id, actor=actor)
    assert service.assert_sendable(draft.id).approved is True


def test_list_approved_is_run_scoped(session: Session, candidate: Candidate, actor: Actor):
    service = DraftService(session)
    first = _create(session, candidate, actor, run_id=None)
    service.approve(first.id, actor=actor)
    assert service.list_approved(run_id="some-run") == []
    assert len(service.list_approved()) == 1


def test_mark_sent_sets_timestamp_and_audit(
    session: Session, candidate: Candidate, actor: Actor
):
    service = DraftService(session)
    draft = _create(session, candidate, actor)
    service.approve(draft.id, actor=actor)
    sent = service.mark_sent(draft.id, actor=actor)
    assert sent.sent_at is not None
    audit = session.query(AuditLog).filter_by(action="draft.sent").one()
    assert audit.entity_id == draft.id


def test_missing_draft_raises(session: Session, actor: Actor):
    with pytest.raises(NotFoundError):
        DraftService(session).get("does-not-exist")


def test_supersede_active_clears_approval(session: Session, candidate: Candidate, actor: Actor):
    service = DraftService(session)
    draft = _create(session, candidate, actor)
    service.approve(draft.id, actor=actor)
    assert service.supersede_active(candidate.id, actor=actor) == 1
    assert service.get(draft.id).approved is False
