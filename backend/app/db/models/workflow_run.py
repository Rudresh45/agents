from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import RunStatus
from app.db.base import Base, UUIDMixin, utcnow


class WorkflowRun(Base, UUIDMixin):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_status", "status"),
        # G7: exactly one active run. active_slot is 1 while running/waiting, NULL otherwise.
        Index(
            "ux_workflow_runs_single_active",
            "active_slot",
            unique=True,
            sqlite_where=text("active_slot IS NOT NULL"),
            postgresql_where=text("active_slot IS NOT NULL"),
        ),
    )

    status: Mapped[str] = mapped_column(String(30), default=RunStatus.RUNNING, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Kestra execution id for the orchestrated run. NULL for legacy LangGraph
    # runs and for a run that failed before Kestra accepted the request.
    kestra_execution_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # 1 while the run is running or waiting for approval, NULL once finished.
    # A partial unique index on this column enforces one active run (G7).
    active_slot: Mapped[int | None] = mapped_column(Integer, default=1, nullable=True)
    triggered_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Set when HR approves the draft batch. Kept after the run resumes so a
    # repeated approve call is a harmless no-op.
    approved_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    counts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    active_job_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    errors: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
