from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class Actor(BaseModel):
    """Who performed an action, for audit rows and service calls."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    name: str = ""
    type: str = "user"

    @classmethod
    def system(cls, name: str = "system") -> Actor:
        return cls(id=name, role="system", name=name, type="system")

    @classmethod
    def agent(cls, name: str) -> Actor:
        return cls(id=name, role="agent", name=name, type="agent")

    @classmethod
    def from_user(cls, user) -> Actor:  # noqa: ANN001
        return cls(
            id=user.id,
            role=user.role,
            name=getattr(user, "full_name", ""),
            type="user",
        )


class JobDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    min_experience_years: int = 0
    education: str | None = None
    location: str | None = None
    min_score: int = 60
    status: str = "open"
    email_alias: str | None = None
    created_by_id: str | None = None
    created_at: datetime | None = None
    closed_at: datetime | None = None


class CandidateDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str | None = None
    full_name: str
    email: str
    phone: str | None = None
    resume_path: str | None = None
    resume_text: str | None = None
    resume_parse_method: str | None = None
    summary: str | None = None
    score: int | None = None
    reason: str | None = None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    scored_model: str | None = None
    prompt_version: str | None = None
    scored_at: datetime | None = None
    status: str
    decision_override_reason: str | None = None
    consent_given: bool = False
    consent_source: str | None = None
    consent_at: datetime | None = None
    retention_until: date | None = None
    anonymised: bool = False
    deletion_requested: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DraftDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    job_id: str | None = None
    run_id: str | None = None
    draft_type: str
    subject: str
    body: str
    version: int = 1
    approved: bool = False
    approved_by_id: str | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    superseded_at: datetime | None = None
    send_error: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    created_at: datetime | None = None


class ReplyDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    category: str
    confidence: float | None = None
    body_excerpt: str | None = None
    handled: bool = False
    handled_at: datetime | None = None
    created_at: datetime | None = None


class RunDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage: str | None = None
    kestra_execution_id: str | None = None
    counts: dict = Field(default_factory=dict)
    active_job_ids: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    dry_run: bool = True
    approved_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    error_message: str | None = None


class ReportItemInput(BaseModel):
    candidate_id: str
    job_title: str = ""
    candidate_name: str = ""
    score: int | None = None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    summary: str | None = None
    decision: str | None = None


class ReportDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str | None = None
    job_id: str | None = None
    file_path: str | None = None
    format: str = "xlsx"
    sent_to: str | None = None
    status: str = "generated"
    candidate_count: int = 0
    created_by_id: str | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None


class JobInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    required_skills: list[str] = Field(min_length=1)
    preferred_skills: list[str] = Field(default_factory=list)
    min_experience_years: int = Field(default=0, ge=0, le=60)
    education: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    min_score: int | None = Field(default=None, ge=0, le=100)
    email_alias: str | None = Field(default=None, max_length=320)


class JobUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    required_skills: list[str] | None = None
    preferred_skills: list[str] | None = None
    min_experience_years: int | None = Field(default=None, ge=0, le=60)
    education: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    min_score: int | None = Field(default=None, ge=0, le=100)
    email_alias: str | None = Field(default=None, max_length=320)


class ApplicationInput(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    job_id: str | None = None
    resume_path: str | None = None
    resume_text: str | None = None
    resume_parse_method: str | None = None
    source_email_id: str | None = None
    consent_given: bool = False
    consent_source: str | None = None
