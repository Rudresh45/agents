from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.constants import CandidateStatus
from app.core.exceptions import NotFoundError, ReasonRequiredError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.schemas.domain import Actor, CandidateDTO
from app.services.audit_service import AuditService
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService

DECISIONS = {CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED}


class ReviewService:
    """HR override of a screening decision. Always audited with a reason."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.candidates = CandidateService(session)
        self.drafts = DraftService(session)
        self.audit = AuditService(session)

    def override_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: str,
        actor: Actor,
    ) -> tuple[CandidateDTO, bool]:
        if decision not in DECISIONS:
            raise ValidationFailedError("Decision must be shortlisted or not_shortlisted")
        if not reason.strip():
            raise ReasonRequiredError("A written reason is required to override a decision")

        current = self.candidates.get(candidate_id)
        if current.status == decision:
            self._record_note(candidate_id, reason, actor)
            return self.candidates.get(candidate_id), False

        self.candidates.transition(candidate_id, decision, actor=actor, reason=reason)
        self._record_note(candidate_id, reason, actor)
        superseded = self.drafts.supersede_active(candidate_id, actor=actor)
        return self.candidates.get(candidate_id), superseded > 0

    def _record_note(self, candidate_id: str, reason: str, actor: Actor) -> None:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        candidate.decision_override_reason = reason
        self.audit.record(
            actor=actor,
            action="candidate.decision_overridden",
            entity_type="candidate",
            entity_id=candidate_id,
            new_value={"decision": candidate.status, "reason": reason},
            reason=reason,
        )
        self.session.commit()
