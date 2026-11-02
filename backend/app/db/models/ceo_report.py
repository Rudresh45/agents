from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin, utcnow


class CeoReport(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "ceo_reports"

    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    format: Mapped[str] = mapped_column(String(10), default="xlsx", nullable=False)
    sent_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="generated", nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[CeoReportItem]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )


class CeoReportItem(Base, UUIDMixin):
    """Snapshot row: later candidate edits must not rewrite a sent report."""

    __tablename__ = "ceo_report_items"

    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ceo_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    job_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    candidate_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    report: Mapped[CeoReport] = relationship(back_populates="items")
