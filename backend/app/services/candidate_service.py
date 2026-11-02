from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    FINAL_STATUS_DRAFT_TYPE,
    CandidateStatus,
    is_transition_allowed,
)
from app.core.exceptions import (
    ApprovalRequiredError,
    DuplicateCandidateError,
    InvalidTransitionError,
    NotFoundError,
    ReasonRequiredError,
    ValidationFailedError,
)
from app.db.models.candidate import Candidate
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from app.db.models.job import Job
from app.schemas.domain import Actor, ApplicationInput, CandidateDTO
from app.services.audit_service import AuditService
from app.utils.resume_parser import parse_resume
from app.utils.storage import Storage

SORTABLE = {
    "created_at": Candidate.created_at,
    "updated_at": Candidate.updated_at,
    "score": Candidate.score,
    "status": Candidate.status,
    "name": Candidate.full_name,
}


def _hash_address(address: str) -> str:
    if not address:
        return ""
    return "sha256:" + hashlib.sha256(address.lower().encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class CandidateService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.settings = get_settings()
        self.storage = Storage(self.settings)

    # --- reads -----------------------------------------------------------
    def get(self, candidate_id: str) -> CandidateDTO:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        return CandidateDTO.model_validate(candidate)

    def find_duplicate(self, email: str, job_id: str | None) -> CandidateDTO | None:
        normalized = email.strip().lower()
        query = select(Candidate).where(func.lower(Candidate.email) == normalized)
        if job_id is None:
            query = query.where(Candidate.job_id.is_(None))
        else:
            query = query.where(Candidate.job_id == job_id)
        candidate = self.session.scalar(query.limit(1))
        return CandidateDTO.model_validate(candidate) if candidate else None

    def find_by_email(self, email: str) -> CandidateDTO | None:
        normalized = email.strip().lower()
        candidate = self.session.scalar(
            select(Candidate)
            .where(func.lower(Candidate.email) == normalized)
            .order_by(Candidate.created_at.desc())
            .limit(1)
        )
        return CandidateDTO.model_validate(candidate) if candidate else None

    def find_by_source_email(self, source_email_id: str) -> CandidateDTO | None:
        """Find the candidate created from one inbound message.

        ``source_email_id`` is an opaque hash, so an apply retry can recover the
        candidate it created before the inbound log row was written.
        """
        if not source_email_id:
            return None
        candidate = self.session.scalar(
            select(Candidate)
            .where(Candidate.source_email_id == source_email_id)
            .limit(1)
        )
        return CandidateDTO.model_validate(candidate) if candidate else None

    def list(
        self,
        *,
        status: str | None = None,
        job_id: str | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        q: str | None = None,
        include_anonymised: bool = False,
        page: int = 1,
        page_size: int = 25,
        sort: str = "-created_at",
    ) -> tuple[list[CandidateDTO], int]:
        filters = []
        if status:
            filters.append(Candidate.status == status)
        if job_id:
            filters.append(Candidate.job_id == job_id)
        if min_score is not None:
            filters.append(Candidate.score >= min_score)
        if max_score is not None:
            filters.append(Candidate.score <= max_score)
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(Candidate.full_name.ilike(pattern), Candidate.email.ilike(pattern))
            )
        if not include_anonymised:
            filters.append(Candidate.anonymised.is_(False))

        total = self.session.scalar(
            select(func.count()).select_from(Candidate).where(*filters)
        ) or 0

        descending = sort.startswith("-")
        column = SORTABLE.get(sort.lstrip("-"), Candidate.created_at)
        order = column.desc() if descending else column.asc()

        rows = self.session.scalars(
            select(Candidate)
            .where(*filters)
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [CandidateDTO.model_validate(row) for row in rows], int(total)

    def counts_by_status(self, job_id: str | None = None) -> dict[str, int]:
        query = select(Candidate.status, func.count()).group_by(Candidate.status)
        if job_id:
            query = query.where(Candidate.job_id == job_id)
        return {status: count for status, count in self.session.execute(query).all()}

    # --- writes ----------------------------------------------------------
    def create_from_application(
        self,
        application: ApplicationInput,
        *,
        actor: Actor,
        run_id: str | None = None,
    ) -> CandidateDTO:
        job = None
        if application.job_id:
            job = self.session.get(Job, application.job_id)
            if job is None:
                raise NotFoundError("Job not found")

        if self.find_duplicate(application.email, application.job_id):
            raise DuplicateCandidateError()

        now = _now()
        candidate = Candidate(
            job_id=application.job_id,
            full_name=application.full_name.strip(),
            email=application.email.strip().lower(),
            phone=application.phone,
            resume_path=application.resume_path,
            resume_text=application.resume_text,
            resume_parse_method=application.resume_parse_method,
            source_email_id=application.source_email_id,
            status=CandidateStatus.RECEIVED,
            consent_given=application.consent_given,
            consent_source=application.consent_source,
            consent_at=now if application.consent_given else None,
            retention_until=date.today() + timedelta(days=self.settings.retention_days),
        )
        self.session.add(candidate)
        self.session.flush()

        self.audit.record(
            actor=actor,
            action="candidate.created",
            entity_type="candidate",
            entity_id=candidate.id,
            new_value={
                "email": candidate.email,
                "job_id": candidate.job_id,
                "status": candidate.status,
            },
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def assign_job(self, candidate_id: str, job_id: str, *, actor: Actor) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        job = self.session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        if job.status != "open":
            raise ValidationFailedError("Cannot assign a closed job")
        if candidate.job_id and candidate.job_id != job_id:
            raise ValidationFailedError(
                "Candidate is already matched to another job (R7)"
            )

        old_job = candidate.job_id
        candidate.job_id = job_id
        if candidate.status == CandidateStatus.NEEDS_REVIEW:
            self._apply_transition(
                candidate,
                CandidateStatus.RECEIVED,
                actor=actor,
                reason="Job assigned by HR",
                run_id=None,
            )
        else:
            self.audit.record(
                actor=actor,
                action="candidate.assigned",
                entity_type="candidate",
                entity_id=candidate.id,
                old_value={"job_id": old_job},
                new_value={"job_id": job_id},
            )
            self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def transition(
        self,
        candidate_id: str,
        new_status: str,
        *,
        actor: Actor,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        self._apply_transition(
            candidate, new_status, actor=actor, reason=reason, run_id=run_id
        )
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def save_score(
        self,
        candidate_id: str,
        *,
        score: int,
        reason: str,
        matched_skills: list[str],
        missing_skills: list[str],
        model_name: str,
        prompt_version: str,
        actor: Actor | None = None,
        run_id: str | None = None,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        if candidate.job_id is None:
            raise ValidationFailedError("Cannot score a candidate without a job (R7)")
        if not 0 <= score <= 100:
            raise ValidationFailedError("Score must be between 0 and 100")

        old = {"score": candidate.score, "reason": candidate.reason}
        candidate.score = score
        candidate.reason = reason
        candidate.matched_skills = matched_skills
        candidate.missing_skills = missing_skills
        candidate.scored_model = model_name
        candidate.prompt_version = prompt_version
        candidate.scored_at = _now()

        self.audit.record(
            actor=actor or Actor.agent("hr_manager_agent"),
            action="candidate.scored",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value=old,
            new_value={"score": score, "reason": reason},
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def set_summary(
        self,
        candidate_id: str,
        summary: str,
        *,
        actor: Actor | None = None,
        run_id: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        cleaned = summary.strip()[:600]
        if candidate.summary == cleaned:
            return CandidateDTO.model_validate(candidate)
        old = candidate.summary
        candidate.summary = cleaned
        self.audit.record(
            actor=actor or Actor.agent("hr_agent"),
            action="candidate.summary_updated",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value={"summary": old},
            new_value={"summary": cleaned},
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def set_full_name(
        self, candidate_id: str, full_name: str, *, actor: Actor | None = None
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        cleaned = full_name.strip()[:200]
        if not cleaned or cleaned == candidate.full_name:
            return CandidateDTO.model_validate(candidate)
        old = candidate.full_name
        candidate.full_name = cleaned
        self.audit.record(
            actor=actor or Actor.agent("hr_agent"),
            action="candidate.name_updated",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value={"full_name": old},
            new_value={"full_name": cleaned},
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def add_error_note(self, candidate_id: str, note: str, actor: Actor) -> None:
        candidate = self._get_model(candidate_id)
        self.audit.record(
            actor=actor,
            action="candidate.processing_error",
            entity_type="candidate",
            entity_id=candidate.id,
            new_value={"note": note[:500]},
        )
        self.session.commit()

    def update_contact(
        self,
        candidate_id: str,
        *,
        full_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        actor: Actor,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        old = {
            "full_name": candidate.full_name,
            "email": candidate.email,
            "phone": candidate.phone,
        }
        if full_name is not None and full_name.strip():
            candidate.full_name = full_name.strip()[:200]
        if email is not None and email.strip():
            candidate.email = email.strip().lower()
        if phone is not None:
            candidate.phone = phone.strip() or None

        self.audit.record(
            actor=actor,
            action="candidate.contact_updated",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value=old,
            new_value={
                "full_name": candidate.full_name,
                "email": candidate.email,
                "phone": candidate.phone,
            },
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def reprocess_resume(
        self,
        candidate_id: str,
        *,
        actor: Actor,
        run_id: str | None = None,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        if not candidate.resume_path:
            raise ValidationFailedError("This candidate has no stored resume")

        parsed = parse_resume(
            self.storage.resolve(candidate.resume_path),
            min_chars=self.settings.resume_min_chars,
        )
        return self.attach_resume(
            candidate_id,
            relative_path=candidate.resume_path,
            text=parsed.text,
            method=parsed.method,
            actor=actor,
            run_id=run_id,
        )

    def attach_resume(
        self,
        candidate_id: str,
        *,
        relative_path: str,
        text: str,
        method: str,
        actor: Actor,
        run_id: str | None = None,
    ) -> CandidateDTO:
        candidate = self._get_model(candidate_id)
        candidate.resume_path = relative_path
        candidate.resume_text = text or None
        candidate.resume_parse_method = method
        self.audit.record(
            actor=actor,
            action="candidate.resume_attached",
            entity_type="candidate",
            entity_id=candidate.id,
            new_value={"method": method, "chars": len(text or "")},
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def withdraw(self, candidate_id: str, *, actor: Actor, reason: str) -> CandidateDTO:
        if not reason.strip():
            raise ReasonRequiredError("A reason is required to withdraw a candidate")
        candidate = self._get_model(candidate_id)
        self._apply_transition(
            candidate,
            CandidateStatus.WITHDRAWN,
            actor=actor,
            reason=reason,
            run_id=None,
        )
        candidate.deletion_requested = True
        self.audit.record(
            actor=actor,
            action="candidate.deletion_flagged",
            entity_type="candidate",
            entity_id=candidate.id,
            new_value={"deletion_requested": True},
            reason=reason,
        )
        self.session.commit()
        self.session.refresh(candidate)
        return CandidateDTO.model_validate(candidate)

    def anonymise(self, candidate_id: str, *, actor: Actor) -> None:
        candidate = self._get_model(candidate_id)
        if candidate.anonymised:
            return
        old = {"email": candidate.email, "full_name": candidate.full_name}
        if candidate.resume_path:
            self.storage.delete(candidate.resume_path)

        candidate.full_name = "Anonymised"
        candidate.email = f"deleted+{candidate.id}@anonymised.invalid"
        candidate.phone = None
        candidate.resume_path = None
        candidate.resume_text = None
        candidate.summary = None
        candidate.anonymised = True
        candidate.status = CandidateStatus.ANONYMISED

        self.audit.record(
            actor=actor,
            action="privacy.anonymised",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value=old,
            new_value={"anonymised": True},
        )
        self.session.commit()

    def hard_delete(self, candidate_id: str, *, actor: Actor) -> None:
        candidate = self._get_model(candidate_id)
        if candidate.resume_path:
            self.storage.delete(candidate.resume_path)

        self.session.query(EmailDraft).filter(
            EmailDraft.candidate_id == candidate_id
        ).delete(synchronize_session=False)
        logs = self.session.scalars(
            select(EmailLog).where(EmailLog.candidate_id == candidate_id)
        ).all()
        for log in logs:
            log.candidate_id = None
            log.draft_id = None
            log.from_address = _hash_address(log.from_address)
            log.to_address = _hash_address(log.to_address)
        from app.db.models.candidate_reply import CandidateReply
        from app.db.models.data_request import DataRequest

        self.session.query(CandidateReply).filter(
            CandidateReply.candidate_id == candidate_id
        ).delete(synchronize_session=False)
        self.session.query(DataRequest).filter(
            DataRequest.candidate_id == candidate_id
        ).delete(synchronize_session=False)

        self.session.delete(candidate)
        self.audit.record(
            actor=actor,
            action="privacy.deleted",
            entity_type="candidate",
            entity_id=candidate_id,
            new_value={"deleted": True},
        )
        self.session.commit()

    # --- internals -------------------------------------------------------
    def _get_model(self, candidate_id: str) -> Candidate:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        return candidate

    def _has_approved_draft(self, candidate_id: str, draft_type: str) -> bool:
        draft = self.session.scalar(
            select(EmailDraft).where(
                EmailDraft.candidate_id == candidate_id,
                EmailDraft.draft_type == draft_type,
                EmailDraft.approved.is_(True),
                EmailDraft.superseded_at.is_(None),
            )
        )
        return draft is not None

    def _apply_transition(
        self,
        candidate: Candidate,
        new_status: str,
        *,
        actor: Actor,
        reason: str | None,
        run_id: str | None,
    ) -> None:
        old_status = candidate.status
        if not is_transition_allowed(old_status, new_status):
            raise InvalidTransitionError(old_status, new_status)

        if (old_status, new_status) in {
            (CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED),
            (CandidateStatus.NOT_SHORTLISTED, CandidateStatus.SHORTLISTED),
        } and not (reason and reason.strip()):
            raise ReasonRequiredError("A written reason is required to override the decision")

        if new_status == CandidateStatus.WITHDRAWN and not (reason and reason.strip()):
            raise ReasonRequiredError("A written reason is required to withdraw")

        if (
            old_status == CandidateStatus.FORWARDED
            and new_status == CandidateStatus.NEEDS_REVIEW
            and not (reason and reason.strip())
        ):
            raise ReasonRequiredError("A written reason is required to unassign a job")

        if (
            new_status == CandidateStatus.RECEIVED
            and old_status == CandidateStatus.NEEDS_REVIEW
            and candidate.job_id is None
        ):
            raise ValidationFailedError("Assign a job before moving the candidate back")

        if (
            new_status == CandidateStatus.RECEIVED
            and old_status == CandidateStatus.NEEDS_RESUBMISSION
            and not candidate.resume_path
        ):
            raise ValidationFailedError("A readable resume is required")

        if new_status in FINAL_STATUS_DRAFT_TYPE:
            required_type = FINAL_STATUS_DRAFT_TYPE[new_status]
            if not self._has_approved_draft(candidate.id, required_type):
                raise ApprovalRequiredError(
                    "An approved draft of the matching type is required (R6)"
                )

        candidate.status = new_status
        self.audit.record(
            actor=actor,
            action="candidate.status_changed",
            entity_type="candidate",
            entity_id=candidate.id,
            old_value={"status": old_status},
            new_value={"status": new_status},
            reason=reason,
            run_id=run_id,
        )
        self.session.commit()
