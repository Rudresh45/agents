from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LLMBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailClassification(LLMBaseModel):
    is_application: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=300)


class ExtractedApplication(LLMBaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    role_title: str | None = Field(default=None, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)


class JobMatch(LLMBaseModel):
    job_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=300)


class ScoreResult(LLMBaseModel):
    score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=600)


class ReplyClassification(LLMBaseModel):
    category: str = Field(pattern="^(accepts|declines|reschedule|question|withdrawn|other)$")
    confidence: float = Field(ge=0.0, le=1.0)


class DraftEmail(LLMBaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class CandidateSummary(LLMBaseModel):
    summary: str = Field(min_length=1, max_length=600)


class CeoSummary(LLMBaseModel):
    summary: str = Field(min_length=1, max_length=400)
