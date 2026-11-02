from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.domain import CandidateDTO, DraftDTO, JobDTO, ReplyDTO, ReportDTO

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


def make_page(items: list[T], total: int, page: int, page_size: int) -> Page[T]:
    pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(
        items=items, total=total, page=page, page_size=page_size, pages=pages
    )


class WorkflowRunRequest(BaseModel):
    force: bool = False
    # Target job id for the screening workflow run.
    job_id: str | None = Field(default=None, max_length=64)


class WorkflowRunResponse(BaseModel):
    run_id: str
    status: str
    kestra_execution_id: str | None = None


class WorkflowApproveRequest(BaseModel):
    run_id: str
    draft_ids: list[str] | None = None
    note: str | None = Field(default=None, max_length=500)


class WorkflowAgentOut(BaseModel):
    state: str
    detail: str


class WorkflowPendingOut(BaseModel):
    drafts: int
    approved: int


class WorkflowStatusOut(BaseModel):
    run_id: str
    status: str
    stage: str
    dry_run: bool
    kestra_execution_id: str | None = None
    kestra_state: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    agents: dict[str, WorkflowAgentOut]
    pending_approval: WorkflowPendingOut
    approved_draft_count: int
    errors: list[dict] = Field(default_factory=list)


class CandidateDetailOut(BaseModel):
    candidate: CandidateDTO
    job: JobDTO | None = None
    drafts: list[DraftDTO] = Field(default_factory=list)
    replies: list[ReplyDTO] = Field(default_factory=list)
    resume_url: str | None = None
    consent_flagged: bool


class DecisionOut(BaseModel):
    candidate: CandidateDTO
    drafts_superseded: bool


class DeleteRequestOut(BaseModel):
    request_id: str
    status: str


class RequestCompletionOut(BaseModel):
    completed: bool
    deleted: bool
    export: dict | None = None


class ReportGenerateOut(BaseModel):
    report_id: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    errors: list[dict] = Field(default_factory=list)
    stop: bool = False


class LogTailOut(BaseModel):
    path: str
    lines: list[str] = Field(default_factory=list)


class NextStatusesOut(BaseModel):
    current: str
    allowed: list[str] = Field(default_factory=list)


class CandidateUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)


class AssignJobRequest(BaseModel):
    job_id: str


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(shortlisted|not_shortlisted)$")
    reason: str = Field(min_length=1, max_length=1000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DraftUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=5000)


class BulkApproveRequest(BaseModel):
    draft_ids: list[str] = Field(min_length=1)


class BulkApproveResult(BaseModel):
    draft_id: str
    approved: bool
    error: str | None = None


class ReplyHandleRequest(BaseModel):
    action: str = Field(pattern="^(mark_handled|withdraw)$")
    reason: str | None = Field(default=None, max_length=1000)


class CeoReportRequest(BaseModel):
    job_ids: list[str] | None = None
    send: bool = True


class ReportItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str | None = None
    job_title: str = ""
    candidate_name: str = ""
    score: int | None = None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    summary: str | None = None
    decision: str | None = None


class ReportDetailOut(ReportDTO):
    items: list[ReportItemOut] = Field(default_factory=list)


class DataRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    request_type: str
    status: str
    notes: str | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    handled_by_id: str | None = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime | None = None
    actor_type: str
    actor_id: str | None = None
    action: str
    entity_type: str
    entity_id: str
    old_value: dict | None = None
    new_value: dict | None = None
    reason: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    run_id: str | None = None
