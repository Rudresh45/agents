from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import DbSession, HrUser
from app.schemas.api import AuditOut, Page, make_page
from app.services.audit_service import AuditService

router = APIRouter(prefix="/audit", tags=["audit"])

Reader = HrUser

COLUMNS = [
    "id",
    "created_at",
    "actor_type",
    "actor_id",
    "action",
    "entity_type",
    "entity_id",
    "old_value",
    "new_value",
    "reason",
    "model_name",
    "prompt_version",
    "run_id",
]


@router.get("", response_model=Page[AuditOut], summary="List audit events")
def list_audit(
    user: Reader,
    db: DbSession,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> Page[AuditOut]:
    rows, total = AuditService(db).list(
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor,
        action=action,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return make_page(
        [AuditOut.model_validate(row) for row in rows], total, page, page_size
    )


@router.get("/export.csv", summary="Export audit events as CSV")
def export_audit(
    user: Reader,
    db: DbSession,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
) -> StreamingResponse:
    service = AuditService(db)

    def rows():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in service.export_rows(
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor,
            action=action,
            date_from=date_from,
            date_to=date_to,
        ):
            writer.writerow(row)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )
