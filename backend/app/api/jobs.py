from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, HrManagerUser, StaffUser, actor_for
from app.schemas.api import Page, make_page
from app.schemas.domain import JobDTO, JobInput, JobUpdate
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])

Reader = StaffUser
Manager = HrManagerUser


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=JobDTO,
    summary="Create a job",
)
def create_job(payload: JobInput, user: Manager, db: DbSession) -> JobDTO:
    return JobService(db).create(payload, actor=actor_for(user))


@router.get("", response_model=Page[JobDTO], summary="List jobs")
def list_jobs(
    user: Reader,
    db: DbSession,
    job_status: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Page[JobDTO]:
    items, total = JobService(db).list(
        status=job_status, q=q, page=page, page_size=page_size
    )
    return make_page(items, total, page, page_size)


@router.get("/{job_id}", response_model=JobDTO, summary="Get a job")
def get_job(job_id: str, user: Reader, db: DbSession) -> JobDTO:
    return JobService(db).get(job_id)


@router.patch("/{job_id}", response_model=JobDTO, summary="Update a job")
def update_job(
    job_id: str, payload: JobUpdate, user: Manager, db: DbSession
) -> JobDTO:
    return JobService(db).update(job_id, payload, actor=actor_for(user))


@router.post("/{job_id}/close", response_model=JobDTO, summary="Close a job")
def close_job(job_id: str, user: Manager, db: DbSession) -> JobDTO:
    return JobService(db).close(job_id, actor=actor_for(user))
