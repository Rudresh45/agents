from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin, utcnow


class EmailLog(Base, UUIDMixin):
    __tablename__ = "email_logs"
    __table_args__ = (
        Index(
            "ux_email_logs_direction_message",
            "direction",
            "message_id",
            unique=True,
            sqlite_where=text("message_id IS NOT NULL"),
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        Index("ix_email_logs_created", "created_at"),
        Index("ix_email_logs_candidate", "candidate_id"),
    )

    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("email_drafts.id", ondelete="SET NULL"), nullable=True
    )
    from_address: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    to_address: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    subject: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
