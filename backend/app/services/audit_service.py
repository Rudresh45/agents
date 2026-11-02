from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.audit_log import AuditLog
from app.schemas.domain import Actor


class AuditService:
    """Append-only audit trail (R8). Only add and read are exposed."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor: Actor,
        action: str,
        entity_type: str,
        entity_id: str,
        old_value: dict | None = None,
        new_value: dict | None = None,
        reason: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        run_id: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.add(entry)
        return entry

    def list(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[AuditLog], int]:
        filters = []
        if entity_type:
            filters.append(AuditLog.entity_type == entity_type)
        if entity_id:
            filters.append(AuditLog.entity_id == entity_id)
        if actor_id:
            filters.append(AuditLog.actor_id == actor_id)
        if action:
            filters.append(AuditLog.action == action)
        if date_from:
            filters.append(AuditLog.created_at >= date_from)
        if date_to:
            filters.append(AuditLog.created_at <= date_to)

        total = self.session.scalar(
            select(func.count()).select_from(AuditLog).where(*filters)
        ) or 0
        rows = (
            self.session.scalars(
                select(AuditLog)
                .where(*filters)
                .order_by(AuditLog.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .all()
        )
        return list(rows), int(total)

    def export_rows(self, **filters) -> Iterator[dict]:
        page = 1
        while True:
            rows, total = self.list(page=page, page_size=200, **filters)
            if not rows:
                return
            for row in rows:
                yield {
                    "id": row.id,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "actor_type": row.actor_type,
                    "actor_id": row.actor_id or "",
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "old_value": row.old_value,
                    "new_value": row.new_value,
                    "reason": row.reason or "",
                    "model_name": row.model_name or "",
                    "prompt_version": row.prompt_version or "",
                    "run_id": row.run_id or "",
                }
            if page * 200 >= total:
                return
            page += 1
