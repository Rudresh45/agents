from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, HrUser, actor_for
from app.core.exceptions import ConflictError, ValidationFailedError
from app.schemas.api import (
    BulkApproveRequest,
    BulkApproveResult,
    DraftUpdate,
    Page,
    ReplyHandleRequest,
    make_page,
)
from app.schemas.domain import DraftDTO, ReplyDTO
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService
from app.services.reply_service import ReplyService

router = APIRouter(tags=["drafts"])

HrOnly = HrUser


@router.get("/drafts", response_model=Page[DraftDTO], summary="List email drafts")
def list_drafts(
    user: HrOnly,
    db: DbSession,
    candidate_id: str | None = None,
    run_id: str | None = None,
    approved: bool | None = None,
    draft_type: str | None = None,
    include_superseded: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Page[DraftDTO]:
    items, total = DraftService(db).list(
        candidate_id=candidate_id,
        run_id=run_id,
        approved=approved,
        draft_type=draft_type,
        include_superseded=include_superseded,
        page=page,
        page_size=page_size,
    )
    return make_page(items, total, page, page_size)


@router.get("/drafts/{draft_id}", response_model=DraftDTO, summary="Get a draft")
def get_draft(draft_id: str, user: HrOnly, db: DbSession) -> DraftDTO:
    return DraftService(db).get(draft_id)


@router.patch(
    "/drafts/{draft_id}",
    response_model=DraftDTO,
    summary="Edit a draft before approval",
)
def update_draft(
    draft_id: str, payload: DraftUpdate, user: HrOnly, db: DbSession
) -> DraftDTO:
    service = DraftService(db)
    draft = service.get(draft_id)
    if draft.sent_at is not None:
        raise ConflictError("A sent draft cannot be changed")
    if payload.subject is None and payload.body is None:
        raise ValidationFailedError("Nothing to update")
    return service.update_draft(
        draft_id,
        subject=payload.subject,
        body=payload.body,
        actor=actor_for(user),
    )


@router.post(
    "/drafts/{draft_id}/approve",
    response_model=DraftDTO,
    summary="Approve one draft (does not send it)",
)
def approve_draft(draft_id: str, user: HrOnly, db: DbSession) -> DraftDTO:
    return DraftService(db).approve(draft_id, actor=actor_for(user))


@router.post(
    "/drafts/{draft_id}/revoke",
    response_model=DraftDTO,
    summary="Revoke approval of an unsent draft",
)
def revoke_draft(draft_id: str, user: HrOnly, db: DbSession) -> DraftDTO:
    return DraftService(db).revoke_approval(draft_id, actor=actor_for(user))


@router.post(
    "/drafts/bulk-approve",
    response_model=list[BulkApproveResult],
    status_code=status.HTTP_200_OK,
    summary="Approve several drafts, reporting per-draft results",
)
def bulk_approve(
    payload: BulkApproveRequest, user: HrOnly, db: DbSession
) -> list[BulkApproveResult]:
    service = DraftService(db)
    results: list[BulkApproveResult] = []
    for draft_id in payload.draft_ids:
        try:
            service.approve(draft_id, actor=actor_for(user))
            results.append(BulkApproveResult(draft_id=draft_id, approved=True))
        except Exception as exc:  # noqa: BLE001 - per-item result is the contract
            db.rollback()
            results.append(
                BulkApproveResult(
                    draft_id=draft_id, approved=False, error=str(exc)[:300]
                )
            )
    return results


@router.get("/replies", response_model=Page[ReplyDTO], summary="List candidate replies")
def list_replies(
    user: HrOnly,
    db: DbSession,
    handled: bool | None = None,
    candidate_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Page[ReplyDTO]:
    items, total = ReplyService(db).list(
        handled=handled,
        candidate_id=candidate_id,
        page=page,
        page_size=page_size,
    )
    return make_page(items, total, page, page_size)


@router.post(
    "/replies/{reply_id}/handle",
    response_model=ReplyDTO,
    summary="Mark a reply handled, optionally withdrawing the candidate",
)
def handle_reply(
    reply_id: str, payload: ReplyHandleRequest, user: HrOnly, db: DbSession
) -> ReplyDTO:
    replies = ReplyService(db)
    reply = replies.get(reply_id)
    if payload.action == "withdraw":
        CandidateService(db).withdraw(
            reply.candidate_id,
            actor=actor_for(user),
            reason=payload.reason or "Withdrawal requested via email reply",
        )
    return replies.mark_handled(reply_id, actor=actor_for(user))
