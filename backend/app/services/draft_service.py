from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import DraftType
from app.core.exceptions import (
    ApprovalRequiredError,
    NotFoundError,
    ValidationFailedError,
)
from app.db.models.candidate import Candidate
from app.db.models.email_draft import EmailDraft
from app.schemas.domain import Actor, DraftDTO
from app.services.audit_service import AuditService

VALID_TYPES = {item.value for item in DraftType}


class DraftService:
    """The only path that can approve or release a candidate email (R6)."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    def get(self, draft_id: str) -> DraftDTO:
        draft = self._get_model(draft_id)
        return DraftDTO.model_validate(draft)

    def list(
        self,
        *,
        candidate_id: str | None = None,
        run_id: str | None = None,
        approved: bool | None = None,
        draft_type: str | None = None,
        include_superseded: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[DraftDTO], int]:
        filters = []
        if candidate_id:
            filters.append(EmailDraft.candidate_id == candidate_id)
        if run_id:
            filters.append(EmailDraft.run_id == run_id)
        if approved is not None:
            filters.append(EmailDraft.approved.is_(approved))
        if draft_type:
            filters.append(EmailDraft.draft_type == draft_type)
        if not include_superseded:
            filters.append(EmailDraft.superseded_at.is_(None))

        total = self.session.scalar(
            select(func.count()).select_from(EmailDraft).where(*filters)
        ) or 0
        rows = self.session.scalars(
            select(EmailDraft)
            .where(*filters)
            .order_by(EmailDraft.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [DraftDTO.model_validate(row) for row in rows], int(total)

    def active_for(self, candidate_id: str, draft_type: str) -> DraftDTO | None:
        draft = self.session.scalar(
            select(EmailDraft).where(
                EmailDraft.candidate_id == candidate_id,
                EmailDraft.draft_type == draft_type,
                EmailDraft.superseded_at.is_(None),
            )
        )
        return DraftDTO.model_validate(draft) if draft else None

    def create_draft(
        self,
        *,
        candidate_id: str,
        draft_type: str,
        subject: str,
        body: str,
        actor: Actor,
        run_id: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> DraftDTO:
        if draft_type not in VALID_TYPES:
            raise ValidationFailedError(f"Unknown draft type: {draft_type}")
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        if not subject.strip() or not body.strip():
            raise ValidationFailedError("A draft needs a subject and a body")

        previous = self.session.scalar(
            select(EmailDraft).where(
                EmailDraft.candidate_id == candidate_id,
                EmailDraft.draft_type == draft_type,
                EmailDraft.superseded_at.is_(None),
            )
        )
        version = 1
        if previous is not None:
            if previous.sent_at is not None:
                raise ValidationFailedError("A sent draft cannot be replaced")
            version = previous.version + 1
            previous.superseded_at = datetime.now(UTC)
            self.audit.record(
                actor=actor,
                action="draft.superseded",
                entity_type="email_draft",
                entity_id=previous.id,
                old_value={"superseded_at": None},
                new_value={"superseded_at": previous.superseded_at.isoformat()},
                reason="Replaced by a newer version",
                run_id=run_id,
            )

        draft = EmailDraft(
            candidate_id=candidate_id,
            job_id=candidate.job_id,
            run_id=run_id,
            draft_type=draft_type,
            subject=subject.strip(),
            body=body.strip(),
            version=version,
            approved=False,
            model_name=model_name,
            prompt_version=prompt_version,
        )
        self.session.add(draft)
        self.session.flush()
        self.audit.record(
            actor=actor,
            action="draft.created",
            entity_type="email_draft",
            entity_id=draft.id,
            new_value={
                "candidate_id": candidate_id,
                "draft_type": draft_type,
                "version": version,
            },
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(draft)
        return DraftDTO.model_validate(draft)

    def update_draft(
        self,
        draft_id: str,
        *,
        subject: str | None,
        body: str | None,
        actor: Actor,
    ) -> DraftDTO:
        draft = self._get_model(draft_id)
        self._assert_editable(draft)
        if subject is None and body is None:
            raise ValidationFailedError("Nothing to update")

        old = {
            "subject": draft.subject,
            "body": draft.body,
            "approved": draft.approved,
        }
        if subject is not None:
            if not subject.strip():
                raise ValidationFailedError("Subject cannot be empty")
            draft.subject = subject.strip()
        if body is not None:
            if not body.strip():
                raise ValidationFailedError("Body cannot be empty")
            draft.body = body.strip()

        if draft.approved:
            # Approval applies to the exact text HR reviewed. Any edit sends the
            # draft back to the approval queue.
            draft.approved = False
            draft.approved_at = None
            draft.approved_by_id = None

        self.audit.record(
            actor=actor,
            action="draft.edited",
            entity_type="email_draft",
            entity_id=draft.id,
            old_value=old,
            new_value={
                "subject": draft.subject,
                "body": draft.body,
                "approved": draft.approved,
            },
            run_id=draft.run_id,
        )
        self.session.commit()
        self.session.refresh(draft)
        return DraftDTO.model_validate(draft)

    def approve(self, draft_id: str, *, actor: Actor) -> DraftDTO:
        draft = self._get_model(draft_id)
        self._assert_editable(draft)
        if draft.approved:
            return DraftDTO.model_validate(draft)
        draft.approved = True
        draft.approved_by_id = actor.id if actor.type == "user" else None
        draft.approved_at = datetime.now(UTC)
        self.audit.record(
            actor=actor,
            action="draft.approved",
            entity_type="email_draft",
            entity_id=draft.id,
            old_value={"approved": False},
            new_value={"approved": True},
            run_id=draft.run_id,
        )
        self.session.commit()
        self.session.refresh(draft)
        return DraftDTO.model_validate(draft)

    def revoke_approval(self, draft_id: str, *, actor: Actor) -> DraftDTO:
        draft = self._get_model(draft_id)
        if draft.sent_at is not None:
            raise ValidationFailedError("A sent draft cannot be changed")
        if not draft.approved:
            return DraftDTO.model_validate(draft)
        draft.approved = False
        draft.approved_by_id = None
        draft.approved_at = None
        self.audit.record(
            actor=actor,
            action="draft.revoked",
            entity_type="email_draft",
            entity_id=draft.id,
            old_value={"approved": True},
            new_value={"approved": False},
            run_id=draft.run_id,
        )
        self.session.commit()
        self.session.refresh(draft)
        return DraftDTO.model_validate(draft)

    def supersede_active(self, candidate_id: str, *, actor: Actor) -> int:
        drafts = self.session.scalars(
            select(EmailDraft).where(
                EmailDraft.candidate_id == candidate_id,
                EmailDraft.superseded_at.is_(None),
                EmailDraft.sent_at.is_(None),
            )
        ).all()
        now = datetime.now(UTC)
        for draft in drafts:
            draft.superseded_at = now
            draft.approved = False
            self.audit.record(
                actor=actor,
                action="draft.superseded",
                entity_type="email_draft",
                entity_id=draft.id,
                old_value={"superseded_at": None},
                new_value={"superseded_at": now.isoformat()},
                reason="Candidate decision changed",
                run_id=draft.run_id,
            )
        if drafts:
            self.session.commit()
        return len(drafts)

    def list_approved(self, *, run_id: str | None = None) -> list[DraftDTO]:
        query = select(EmailDraft).where(
            EmailDraft.approved.is_(True),
            EmailDraft.sent_at.is_(None),
            EmailDraft.superseded_at.is_(None),
        )
        if run_id:
            query = query.where(EmailDraft.run_id == run_id)
        rows = self.session.scalars(query.order_by(EmailDraft.created_at.asc())).all()
        return [DraftDTO.model_validate(row) for row in rows]

    def assert_sendable(self, draft_id: str) -> DraftDTO:
        draft = self._get_model(draft_id)
        if draft.superseded_at is not None:
            raise ValidationFailedError("This draft has been superseded")
        if not draft.approved:
            raise ApprovalRequiredError("Draft is not approved (R6)")
        if draft.sent_at is not None:
            raise ValidationFailedError("Draft has already been sent")
        return DraftDTO.model_validate(draft)

    def mark_sent(self, draft_id: str, *, actor: Actor) -> DraftDTO:
        draft = self._get_model(draft_id)
        if draft.sent_at is not None:
            return DraftDTO.model_validate(draft)
        draft.sent_at = datetime.now(UTC)
        draft.send_error = None
        self.audit.record(
            actor=actor,
            action="draft.sent",
            entity_type="email_draft",
            entity_id=draft.id,
            old_value={"sent_at": None},
            new_value={"sent_at": draft.sent_at.isoformat()},
            run_id=draft.run_id,
        )
        self.session.commit()
        self.session.refresh(draft)
        return DraftDTO.model_validate(draft)

    def mark_failed(self, draft_id: str, *, error: str, actor: Actor) -> None:
        draft = self._get_model(draft_id)
        draft.send_error = error[:1000]
        self.audit.record(
            actor=actor,
            action="draft.send_failed",
            entity_type="email_draft",
            entity_id=draft.id,
            new_value={"error": error[:500]},
            run_id=draft.run_id,
        )
        self.session.commit()

    def _get_model(self, draft_id: str) -> EmailDraft:
        draft = self.session.get(EmailDraft, draft_id)
        if draft is None:
            raise NotFoundError("Draft not found")
        return draft

    @staticmethod
    def _assert_editable(draft: EmailDraft) -> None:
        if draft.sent_at is not None:
            raise ValidationFailedError("A sent draft cannot be changed")
        if draft.superseded_at is not None:
            raise ValidationFailedError("A superseded draft cannot be changed")
