from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import CandidateStatus
from app.db.base import Base, TimestampMixin, UUIDMixin


class Candidate(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("email", "job_id", name="uq_candidates_email_job"),
        Index("ix_candidates_status", "status"),
        Index("ix_candidates_job_status", "job_id", "status"),
        Index("ix_candidates_score", "score"),
        Index("ix_candidates_retention", "retention_until"),
    )

    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Deliberately no FK to email_logs: candidates <-> email_logs would be a cycle
    # that breaks migration ordering. Provenance is resolved through email_logs.candidate_id.
    source_email_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    resume_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_parse_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scored_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(30), default=CandidateStatus.RECEIVED, nullable=False
    )
    decision_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    consent_given: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    anonymised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deletion_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Candidate {self.email} ({self.status})>"
