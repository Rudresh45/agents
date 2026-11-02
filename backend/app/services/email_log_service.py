from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.email_log import EmailLog
from app.schemas.domain import Actor


class EmailLogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def exists_message_id(self, message_id: str, *, direction: str = "inbound") -> bool:
        if not message_id:
            return False
        return (
            self.session.scalar(
                select(EmailLog.id).where(
                    EmailLog.direction == direction,
                    EmailLog.message_id == message_id,
                )
            )
            is not None
        )

    def find_inbound(self, message_id: str) -> EmailLog | None:
        if not message_id:
            return None
        return self.session.scalar(
            select(EmailLog)
            .where(
                EmailLog.direction == "inbound",
                EmailLog.message_id == message_id,
            )
            .limit(1)
        )

    def record_inbound(
        self,
        *,
        message_id: str | None,
        from_address: str,
        to_address: str,
        subject: str,
        status: str,
        run_id: str | None = None,
        candidate_id: str | None = None,
        error: str | None = None,
    ) -> EmailLog:
        log = EmailLog(
            direction="inbound",
            message_id=message_id or None,
            run_id=run_id,
            candidate_id=candidate_id,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            status=status,
            error=error,
        )
        self.session.add(log)
        self.session.commit()
        return log

    def record_outbound(
        self,
        *,
        to_address: str,
        subject: str,
        status: str,
        actor: Actor,
        run_id: str | None = None,
        candidate_id: str | None = None,
        draft_id: str | None = None,
        message_id: str | None = None,
        from_address: str = "",
        error: str | None = None,
    ) -> EmailLog:
        log = EmailLog(
            direction="outbound",
            message_id=message_id or None,
            run_id=run_id,
            candidate_id=candidate_id,
            draft_id=draft_id,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            status=status,
            error=error,
        )
        self.session.add(log)
        self.session.commit()
        return log

    def list(
        self,
        *,
        direction: str | None = None,
        status: str | None = None,
        candidate_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[EmailLog], int]:
        filters = []
        if direction:
            filters.append(EmailLog.direction == direction)
        if status:
            filters.append(EmailLog.status == status)
        if candidate_id:
            filters.append(EmailLog.candidate_id == candidate_id)
        total = self.session.scalar(
            select(func.count()).select_from(EmailLog).where(*filters)
        ) or 0
        rows = self.session.scalars(
            select(EmailLog)
            .where(*filters)
            .order_by(EmailLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total)
