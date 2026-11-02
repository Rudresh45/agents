from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.exceptions import AuditImmutableError
from app.db.base import Base, UUIDMixin, utcnow


class AuditLog(Base, UUIDMixin):
    """Append-only history (R8). No update or delete path exists in code."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_created", "created_at"),
        Index("ix_audit_logs_run", "run_id"),
    )

    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


@event.listens_for(AuditLog, "before_update")
def _prevent_audit_update(_mapper, _connection, _target) -> None:  # noqa: ANN001
    raise AuditImmutableError("Audit entries cannot be modified")


@event.listens_for(AuditLog, "before_delete")
def _prevent_audit_delete(_mapper, _connection, _target) -> None:  # noqa: ANN001
    raise AuditImmutableError("Audit entries cannot be deleted")
