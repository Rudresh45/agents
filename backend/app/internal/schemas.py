from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.llm import (
    CandidateSummary,
    CeoSummary,
    DraftEmail,
    EmailClassification,
    JobMatch,
    ReplyClassification,
    ScoreResult,
)


class InternalPingOut(BaseModel):
    status: str = "ok"
    service: str = "backend"


class ScreenPrepareIn(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=200, ge=1, le=500)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class ScreenPrepareItemOut(BaseModel):
    candidate_id: str
    job_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


class ScreenPrepareOut(BaseModel):
    items: list[ScreenPrepareItemOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    has_work: bool = False
    stopped: bool = False


class ScreenApplyItemIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    result: ScoreResult
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)

    @field_validator("result", mode="before")
    @classmethod
    def _parse_json_text(cls, value: Any) -> Any:
        """Accept Kestra's JSON output object or its raw JSON text."""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("result must be valid JSON") from exc
        return value


class ScreenApplyIn(BaseModel):
    items: list[ScreenApplyItemIn] = Field(default_factory=list, max_length=500)
    job_id: str | None = Field(default=None, min_length=1, max_length=36)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    finalize: bool = True

    @model_validator(mode="after")
    def _require_work(self) -> ScreenApplyIn:
        if not self.items and not self.finalize:
            raise ValueError("Provide at least one item or set finalize=true")
        return self


class ScreenApplyOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    shortlist: dict[str, Any] = Field(default_factory=dict)
    finalized: bool = False


class PromptBlockOut(BaseModel):
    system_prompt: str
    user_prompt: str
    prompt_version: str = Field(min_length=1, max_length=50)
    response_schema: dict[str, Any] = Field(default_factory=dict)


class InboxApplicationItemOut(BaseModel):
    email_key: str
    job_id: str | None = None
    classification: PromptBlockOut
    job_match: PromptBlockOut | None = None


class InboxReplyItemOut(BaseModel):
    email_key: str
    reply: PromptBlockOut


class InboxPrepareIn(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class InboxPrepareOut(BaseModel):
    # ``applications`` is the full list. The two split lists let Kestra run a
    # job-match AI task only for items that actually need one; an alias-matched
    # item must never reference a skipped task output.
    applications: list[InboxApplicationItemOut] = Field(default_factory=list)
    alias_applications: list[InboxApplicationItemOut] = Field(default_factory=list)
    ai_applications: list[InboxApplicationItemOut] = Field(default_factory=list)
    replies: list[InboxReplyItemOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    has_work: bool = False


class InboxApplyIn(BaseModel):
    email_key: str = Field(min_length=1, max_length=64)
    classification: EmailClassification
    classification_model: str = Field(min_length=1, max_length=100)
    classification_prompt_version: str = Field(min_length=1, max_length=50)
    job_match: JobMatch | None = None
    job_match_model: str | None = Field(default=None, min_length=1, max_length=100)
    job_match_prompt_version: str | None = Field(
        default=None, min_length=1, max_length=50
    )
    run_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("classification", mode="before")
    @classmethod
    def _parse_classification(cls, value: Any) -> Any:
        return _loads(value, "classification")

    @field_validator("job_match", mode="before")
    @classmethod
    def _parse_job_match(cls, value: Any) -> Any:
        return _loads(value, "job_match") if value is not None else None


class InboxApplyOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    candidate_id: str | None = None
    draft_type: str | None = None
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class InboxReplyApplyIn(BaseModel):
    email_key: str = Field(min_length=1, max_length=64)
    reply: ReplyClassification
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("reply", mode="before")
    @classmethod
    def _parse_reply(cls, value: Any) -> Any:
        return _loads(value, "reply")


class InboxAcknowledgeIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    draft_type: str | None = Field(default=None, max_length=40)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class InboxAcknowledgeOut(BaseModel):
    created: bool = False
    draft_id: str | None = None
    skipped: bool = False
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ForwardPrepareIn(BaseModel):
    limit: int = Field(default=200, ge=1, le=500)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class ForwardPrepareItemOut(BaseModel):
    candidate_id: str
    job_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


class ForwardPrepareOut(BaseModel):
    items: list[ForwardPrepareItemOut] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    has_work: bool = False


class ForwardApplyItemIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    result: CandidateSummary
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)

    @field_validator("result", mode="before")
    @classmethod
    def _parse_result(cls, value: Any) -> Any:
        return _loads(value, "result")


class ForwardApplyIn(BaseModel):
    items: list[ForwardApplyItemIn] = Field(default_factory=list, max_length=500)
    candidate_ids: list[str] = Field(default_factory=list, max_length=500)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    finalize: bool = True

    @model_validator(mode="after")
    def _require_work(self) -> ForwardApplyIn:
        if not self.items and not self.finalize:
            raise ValueError("Provide at least one item or set finalize=true")
        return self


class ForwardApplyOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class DraftPrepareIn(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=200, ge=1, le=500)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class DraftPrepareItemOut(BaseModel):
    candidate_id: str
    job_id: str
    draft_type: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


class DraftPrepareOut(BaseModel):
    items: list[DraftPrepareItemOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    has_work: bool = False


class DraftApplyItemIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    draft_type: str = Field(min_length=1, max_length=40)
    result: DraftEmail
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)

    @field_validator("result", mode="before")
    @classmethod
    def _parse_result(cls, value: Any) -> Any:
        return _loads(value, "result")


class DraftApplyIn(BaseModel):
    items: list[DraftApplyItemIn] = Field(default_factory=list, max_length=500)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def _require_work(self) -> DraftApplyIn:
        if not self.items:
            raise ValueError("Provide at least one item")
        return self


class DraftApplyOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class SendApprovedIn(BaseModel):
    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    draft_ids: list[str] | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


class SendApprovedOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ReportPrepareIn(BaseModel):
    job_ids: list[str] | None = Field(default=None, max_length=100)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


class ReportPrepareItemOut(BaseModel):
    candidate_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


class ReportPrepareOut(BaseModel):
    items: list[ReportPrepareItemOut] = Field(default_factory=list)
    job_ids: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    stopped: bool = False
    has_work: bool = False


class ReportApplyItemIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    result: CeoSummary
    model_name: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)

    @field_validator("result", mode="before")
    @classmethod
    def _parse_result(cls, value: Any) -> Any:
        return _loads(value, "result")


class ReportApplyIn(BaseModel):
    items: list[ReportApplyItemIn] = Field(default_factory=list, max_length=500)
    job_ids: list[str] | None = Field(default=None, max_length=100)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    finalize: bool = True
    send: bool = True

    @model_validator(mode="after")
    def _require_work(self) -> ReportApplyIn:
        if not self.items and not self.finalize:
            raise ValueError("Provide at least one item or set finalize=true")
        return self


class ReportApplyOut(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    report_id: str | None = None


class AlertIn(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=300)
    detail: str = Field(min_length=1, max_length=4000)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)


class AlertOut(BaseModel):
    sent: bool = False


def _loads(value: Any, field: str) -> Any:
    """Accept Kestra's JSON object or its raw JSON text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be valid JSON") from exc
    return value
