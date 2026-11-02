from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession, HrUser, actor_for
from app.core.constants import DataRequestType
from app.core.exceptions import ValidationFailedError
from app.schemas.api import DataRequestOut, RequestCompletionOut
from app.services.privacy_service import PrivacyService

router = APIRouter(prefix="/privacy", tags=["privacy"])

HrOnly = HrUser


@router.get(
    "/requests",
    response_model=list[DataRequestOut],
    summary="List data access and deletion requests",
)
def list_requests(
    user: HrOnly,
    db: DbSession,
    request_status: str | None = Query(default=None, alias="status"),
) -> list[DataRequestOut]:
    rows = PrivacyService(db).list_requests(status=request_status)
    return [DataRequestOut.model_validate(row) for row in rows]


@router.post(
    "/requests",
    response_model=DataRequestOut,
    summary="Open a data request for a candidate",
)
def create_request(
    candidate_id: str,
    request_type: str,
    user: HrOnly,
    db: DbSession,
) -> DataRequestOut:
    if request_type not in {item.value for item in DataRequestType}:
        raise ValidationFailedError("Request type must be access or deletion")
    row = PrivacyService(db).create_request(
        candidate_id=candidate_id,
        request_type=request_type,
        actor=actor_for(user),
    )
    return DataRequestOut.model_validate(row)


@router.post(
    "/requests/{request_id}/complete",
    response_model=RequestCompletionOut,
    summary="Complete a data request (export or erase)",
)
def complete_request(
    request_id: str, user: HrOnly, db: DbSession
) -> RequestCompletionOut:
    export = PrivacyService(db).complete_request(request_id, actor=actor_for(user))
    if export is None:
        return RequestCompletionOut(completed=True, deleted=True)
    return RequestCompletionOut(completed=True, deleted=False, export=export)


@router.get(
    "/export/{candidate_id}",
    summary="Export everything stored about one candidate",
)
def export_candidate(candidate_id: str, user: HrOnly, db: DbSession) -> dict:
    return PrivacyService(db).export_candidate_data(candidate_id)


@router.post(
    "/sweep",
    summary="Delete candidates past their retention date",
)
def retention_sweep(user: HrOnly, db: DbSession) -> dict:
    deleted = PrivacyService(db).retention_sweep(actor=actor_for(user))
    return {"deleted": deleted}
