from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import ReplyCategory
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.db.models.candidate_reply import CandidateReply
from app.schemas.domain import Actor, ReplyDTO
from app.services.audit_service import AuditService
from app.services.candidate_service import CandidateService

VALID_CATEGORIES = {item.value for item in ReplyCategory}


class ReplyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.candidates = CandidateService(session)

    def record(
        self,
        *,
        candidate_id: str,
        category: str,
        actor: Actor,
        confidence: float | None = None,
        body_excerpt: str | None = None,
        email_log_id: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        run_id: str | None = None,
    ) -> ReplyDTO:
        if category not in VALID_CATEGORIES:
            raise ValidationFailedError(f"Unknown reply category: {category}")
        if self.session.get(Candidate, candidate_id) is None:
            raise NotFoundError("Candidate not found")

        reply = CandidateReply(
            candidate_id=candidate_id,
            email_log_id=email_log_id,
            category=category,
            confidence=confidence,
            body_excerpt=(body_excerpt or "")[:2000] or None,
        )
        self.session.add(reply)
        self.session.flush()
        self.audit.record(
            actor=actor,
            action="reply.recorded",
            entity_type="candidate_reply",
            entity_id=reply.id,
            new_value={"candidate_id": candidate_id, "category": category},
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.commit()

        if category == ReplyCategory.WITHDRAWN:
            self.candidates.withdraw(
                candidate_id,
                actor=actor,
                reason="Candidate withdrew via email reply",
            )

        self.session.refresh(reply)
        return ReplyDTO.model_validate(reply)

    def list(
        self,
        *,
        handled: bool | None = None,
        candidate_id: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[ReplyDTO], int]:
        filters = []
        if handled is not None:
            filters.append(CandidateReply.handled.is_(handled))
        if candidate_id:
            filters.append(CandidateReply.candidate_id == candidate_id)
        total = self.session.scalar(
            select(func.count()).select_from(CandidateReply).where(*filters)
        ) or 0
        rows = self.session.scalars(
            select(CandidateReply)
            .where(*filters)
            .order_by(CandidateReply.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [ReplyDTO.model_validate(row) for row in rows], int(total)

    def get(self, reply_id: str) -> ReplyDTO:
        reply = self.session.get(CandidateReply, reply_id)
        if reply is None:
            raise NotFoundError("Reply not found")
        return ReplyDTO.model_validate(reply)

    def mark_handled(self, reply_id: str, *, actor: Actor) -> ReplyDTO:
        reply = self.session.get(CandidateReply, reply_id)
        if reply is None:
            raise NotFoundError("Reply not found")
        if reply.handled:
            return ReplyDTO.model_validate(reply)
        reply.handled = True
        reply.handled_by_id = actor.id if actor.type == "user" else None
        reply.handled_at = datetime.now(UTC)
        self.audit.record(
            actor=actor,
            action="reply.handled",
            entity_type="candidate_reply",
            entity_id=reply.id,
            old_value={"handled": False},
            new_value={"handled": True},
        )
        self.session.commit()
        self.session.refresh(reply)
        return ReplyDTO.model_validate(reply)
