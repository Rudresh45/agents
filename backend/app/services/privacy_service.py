from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import CandidateStatus, DataRequestStatus, DataRequestType
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.db.models.candidate_reply import CandidateReply
from app.db.models.data_request import DataRequest
from app.db.models.email_draft import EmailDraft
from app.schemas.domain import Actor, CandidateDTO, DraftDTO, ReplyDTO
from app.services.audit_service import AuditService
from app.services.candidate_service import CandidateService

VALID_TYPES = {item.value for item in DataRequestType}


class PrivacyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.candidates = CandidateService(session)

    def create_request(
        self,
        *,
        candidate_id: str,
        request_type: str,
        actor: Actor,
        notes: str | None = None,
    ) -> DataRequest:
        if request_type not in VALID_TYPES:
            raise ValidationFailedError("Request type must be access or deletion")
        if self.session.get(Candidate, candidate_id) is None:
            raise NotFoundError("Candidate not found")

        request = DataRequest(
            candidate_id=candidate_id,
            request_type=request_type,
            status=DataRequestStatus.OPEN,
            notes=notes,
        )
        self.session.add(request)
        self.session.flush()
        self.audit.record(
            actor=actor,
            action="privacy.request_created",
            entity_type="data_request",
            entity_id=request.id,
            new_value={"candidate_id": candidate_id, "type": request_type},
        )
        self.session.commit()
        self.session.refresh(request)
        return request

    def list_requests(self, *, status: str | None = None) -> list[DataRequest]:
        query = select(DataRequest).order_by(DataRequest.requested_at.desc())
        if status:
            query = query.where(DataRequest.status == status)
        return list(self.session.scalars(query).all())

    def complete_request(self, request_id: str, *, actor: Actor) -> dict | None:
        request = self.session.get(DataRequest, request_id)
        if request is None:
            raise NotFoundError("Data request not found")
        if request.status != DataRequestStatus.OPEN:
            raise ValidationFailedError("This request is already closed")

        if request.request_type == DataRequestType.DELETION:
            candidate_id = request.candidate_id
            self.candidates.hard_delete(candidate_id, actor=actor)
            return None

        export = self.export_candidate_data(request.candidate_id)
        request.status = DataRequestStatus.COMPLETED
        request.completed_at = datetime.now(UTC)
        request.handled_by_id = actor.id if actor.type == "user" else None
        self.audit.record(
            actor=actor,
            action="privacy.request_completed",
            entity_type="data_request",
            entity_id=request.id,
            new_value={"type": request.request_type},
        )
        self.session.commit()
        return export

    def export_candidate_data(self, candidate_id: str) -> dict:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        drafts = self.session.scalars(
            select(EmailDraft).where(EmailDraft.candidate_id == candidate_id)
        ).all()
        replies = self.session.scalars(
            select(CandidateReply).where(CandidateReply.candidate_id == candidate_id)
        ).all()
        return {
            "candidate": CandidateDTO.model_validate(candidate).model_dump(mode="json"),
            "drafts": [DraftDTO.model_validate(row).model_dump(mode="json") for row in drafts],
            "replies": [ReplyDTO.model_validate(row).model_dump(mode="json") for row in replies],
            "exported_at": datetime.now(UTC).isoformat(),
        }

    def retention_sweep(self, *, today: date | None = None, actor: Actor | None = None) -> int:
        cutoff = today or date.today()
        rows = self.session.scalars(
            select(Candidate).where(
                Candidate.retention_until.is_not(None),
                Candidate.retention_until <= cutoff,
                Candidate.anonymised.is_(False),
                Candidate.status != CandidateStatus.DELETED,
            )
        ).all()

        system = actor or Actor.system("retention")
        for candidate in rows:
            self.candidates.anonymise(candidate.id, actor=system)
        return len(rows)
