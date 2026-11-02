from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin, utcnow


class CandidateReply(Base, UUIDMixin):
    __tablename__ = "candidate_replies"
    __table_args__ = (
        Index("ix_candidate_replies_candidate", "candidate_id"),
        Index("ix_candidate_replies_handled", "handled"),
    )

    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    email_log_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("email_logs.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handled_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
