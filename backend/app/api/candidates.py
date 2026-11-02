from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, status
from fastapi.responses import FileResponse

from app.api.deps import DbSession, HrOrManager, HrUser, actor_for
from app.core.constants import ALLOWED_TRANSITIONS, DataRequestType
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.schemas.api import (
    AssignJobRequest,
    CandidateDetailOut,
    CandidateUpdate,
    DecisionOut,
    DecisionRequest,
    DeleteRequestOut,
    NextStatusesOut,
    Page,
    ReasonRequest,
    make_page,
)
from app.schemas.domain import CandidateDTO, DraftDTO, ReplyDTO
from app.services.candidate_service import SORTABLE, CandidateService
from app.services.draft_service import DraftService
from app.services.job_service import JobService
from app.services.privacy_service import PrivacyService
from app.services.reply_service import ReplyService
from app.services.review_service import ReviewService

router = APIRouter(prefix="/candidates", tags=["candidates"])

Reader = HrOrManager
HrOnly = HrUser


@router.get("", response_model=Page[CandidateDTO], summary="List candidates")
def list_candidates(
    user: Reader,
    db: DbSession,
    candidate_status: str | None = Query(default=None, alias="status"),
    job_id: str | None = None,
    min_score: int | None = Query(default=None, ge=0, le=100),
    max_score: int | None = Query(default=None, ge=0, le=100),
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "-created_at",
) -> Page[CandidateDTO]:
    if sort.lstrip("-") not in SORTABLE:
        raise ValidationFailedError("Unsupported sort field")
    items, total = CandidateService(db).list(
        status=candidate_status,
        job_id=job_id,
        min_score=min_score,
        max_score=max_score,
        q=q,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return make_page(items, total, page, page_size)


@router.get(
    "/{candidate_id}",
    response_model=CandidateDetailOut,
    summary="Candidate detail",
)
def get_candidate(
    candidate_id: str, user: Reader, db: DbSession
) -> CandidateDetailOut:
    candidate = CandidateService(db).get(candidate_id)
    job = JobService(db).get(candidate.job_id) if candidate.job_id else None
    drafts, _total = DraftService(db).list(candidate_id=candidate_id, page_size=100)
    replies, _total = ReplyService(db).list(candidate_id=candidate_id, page_size=100)
    return {
        "candidate": candidate,
        "job": job,
        "drafts": [DraftDTO.model_validate(item) for item in drafts],
        "replies": [ReplyDTO.model_validate(item) for item in replies],
        "resume_url": (
            f"/api/v1/candidates/{candidate_id}/resume"
            if candidate.resume_path
            else None
        ),
        "consent_flagged": not candidate.consent_given,
    }


@router.patch(
    "/{candidate_id}",
    response_model=CandidateDTO,
    summary="Correct candidate contact details",
)
def update_candidate(
    candidate_id: str, payload: CandidateUpdate, user: HrOnly, db: DbSession
) -> CandidateDTO:
    if payload.full_name is None and payload.email is None and payload.phone is None:
        raise ValidationFailedError("Nothing to update")
    return CandidateService(db).update_contact(
        candidate_id,
        full_name=payload.full_name,
        email=str(payload.email) if payload.email else None,
        phone=payload.phone,
        actor=actor_for(user),
    )


@router.post(
    "/{candidate_id}/assign-job",
    response_model=CandidateDTO,
    summary="Assign a job to a candidate under review",
)
def assign_job(
    candidate_id: str, payload: AssignJobRequest, user: HrOnly, db: DbSession
) -> CandidateDTO:
    return CandidateService(db).assign_job(
        candidate_id, payload.job_id, actor=actor_for(user)
    )


@router.post(
    "/{candidate_id}/decision",
    response_model=DecisionOut,
    summary="Override the screening decision (requires a reason)",
)
def decide(
    candidate_id: str, payload: DecisionRequest, user: HrOnly, db: DbSession
) -> DecisionOut:
    candidate, superseded = ReviewService(db).override_decision(
        candidate_id,
        decision=payload.decision,
        reason=payload.reason,
        actor=actor_for(user),
    )
    return {"candidate": candidate, "drafts_superseded": superseded}


@router.post(
    "/{candidate_id}/withdraw",
    response_model=CandidateDTO,
    summary="Mark a candidate as withdrawn",
)
def withdraw(
    candidate_id: str, payload: ReasonRequest, user: HrOnly, db: DbSession
) -> CandidateDTO:
    return CandidateService(db).withdraw(
        candidate_id, actor=actor_for(user), reason=payload.reason
    )


@router.get(
    "/{candidate_id}/resume",
    summary="Download a candidate resume",
    response_class=FileResponse,
)
def download_resume(candidate_id: str, user: Reader, db: DbSession) -> FileResponse:
    candidate = CandidateService(db).get(candidate_id)
    if not candidate.resume_path:
        raise ValidationFailedError("This candidate has no stored resume")
    service = CandidateService(db)
    path = service.storage.resolve(candidate.resume_path)
    if not path.is_file():
        raise NotFoundError("Resume file is missing")
    return FileResponse(
        path,
        media_type=(
            "application/pdf"
            if path.suffix.lower() == ".pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=Path(path).name,
    )


@router.post(
    "/{candidate_id}/reprocess",
    response_model=CandidateDTO,
    summary="Re-parse the stored resume (does not re-score)",
)
def reprocess(candidate_id: str, user: HrOnly, db: DbSession) -> CandidateDTO:
    return CandidateService(db).reprocess_resume(
        candidate_id, actor=actor_for(user)
    )


@router.delete(
    "/{candidate_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeleteRequestOut,
    summary="Queue a deletion request",
)
def delete_candidate(
    candidate_id: str, user: HrOnly, db: DbSession
) -> DeleteRequestOut:
    request = PrivacyService(db).create_request(
        candidate_id=candidate_id,
        request_type=DataRequestType.DELETION,
        actor=actor_for(user),
        notes=f"Requested from candidate detail by {user.email}",
    )
    return {"request_id": request.id, "status": request.status}


@router.get(
    "/{candidate_id}/statuses",
    response_model=NextStatusesOut,
    summary="Allowed next statuses for this candidate",
)
def next_statuses(
    candidate_id: str, user: Reader, db: DbSession
) -> NextStatusesOut:
    candidate = CandidateService(db).get(candidate_id)
    return {
        "current": candidate.status,
        "allowed": sorted(ALLOWED_TRANSITIONS.get(candidate.status, frozenset())),
    }
