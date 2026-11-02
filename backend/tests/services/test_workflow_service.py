from __future__ import annotations

import pytest
from app.core.constants import RunStatus
from app.core.exceptions import (
    ApprovalRequiredError,
    RunAlreadyActiveError,
    ValidationFailedError,
)
from app.schemas.domain import Actor
from app.services.workflow_service import WorkflowService
from sqlalchemy.orm import Session


def test_start_creates_running_run(session: Session, actor: Actor):
    run = WorkflowService(session).start(actor=actor, dry_run=True)
    assert run.status == RunStatus.RUNNING
    assert run.dry_run is True
    assert run.started_at is not None


def test_second_active_run_is_rejected(session: Session, actor: Actor):
    service = WorkflowService(session)
    service.start(actor=actor)
    with pytest.raises(RunAlreadyActiveError):
        service.start(actor=actor)


def test_finish_frees_the_slot(session: Session, actor: Actor):
    service = WorkflowService(session)
    first = service.start(actor=actor)
    service.finish(first.id)
    second = service.start(actor=actor)
    assert second.id != first.id


def test_cancel_frees_the_slot(session: Session, actor: Actor):
    service = WorkflowService(session)
    run = service.start(actor=actor)
    cancelled = service.cancel(run.id, actor=actor)
    assert cancelled.status == RunStatus.CANCELLED
    service.start(actor=actor)


def test_fail_records_error_and_frees_slot(session: Session, actor: Actor):
    service = WorkflowService(session)
    run = service.start(actor=actor)
    failed = service.fail(run.id, error="LLM down")
    assert failed.status == RunStatus.FAILED
    assert failed.error_message == "LLM down"
    service.start(actor=actor)


def test_approve_requires_waiting_status(session: Session, actor: Actor):
    service = WorkflowService(session)
    run = service.start(actor=actor)
    with pytest.raises(ValidationFailedError):
        service.approve(run.id, actor=actor)


def test_approve_requires_an_approved_draft(session: Session, actor: Actor, candidate):
    from app.core.constants import DraftType
    from app.services.draft_service import DraftService

    service = WorkflowService(session)
    run = service.start(actor=actor)
    service.set_waiting_approval(run.id)
    with pytest.raises(ApprovalRequiredError):
        service.approve(run.id, actor=actor)

    drafts = DraftService(session)
    draft = drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Come in",
        actor=Actor.agent("hr_agent"),
        run_id=run.id,
    )
    drafts.approve(draft.id, actor=actor)
    assert service.approve(run.id, actor=actor) == 1
    assert service.approve(run.id, actor=actor) == 0

    # Revoking the only approved draft blocks a repeated Kestra resume (R6).
    drafts.revoke_approval(draft.id, actor=actor)
    with pytest.raises(ApprovalRequiredError):
        service.approve(run.id, actor=actor)


def test_heartbeat_and_error_collection(session: Session, actor: Actor):
    service = WorkflowService(session)
    run = service.start(actor=actor)
    service.heartbeat(run.id)
    service.add_error(run.id, node="read_emails", error="bad message", item="uid-1")
    updated = service.get(run.id)
    assert updated.last_heartbeat_at is not None
    assert updated.errors[0]["node"] == "read_emails"


def test_active_run_lookup(session: Session, actor: Actor):
    service = WorkflowService(session)
    assert service.active_run() is None
    run = service.start(actor=actor)
    assert service.active_run().id == run.id
    service.finish(run.id)
    assert service.active_run() is None
