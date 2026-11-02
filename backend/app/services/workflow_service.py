from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import ACTIVE_RUN_STATUSES, RunStatus
from app.core.exceptions import (
    ApprovalRequiredError,
    NotFoundError,
    RunAlreadyActiveError,
    ValidationFailedError,
)
from app.db.models.workflow_run import WorkflowRun
from app.schemas.domain import Actor, RunDTO
from app.services.audit_service import AuditService
from app.services.draft_service import DraftService

# Kestra state names are uppercase. CREATED/QUEUED/RETRYING are all still
# active work from the user's point of view.
KESTRA_RUNNING_STATES = frozenset({"CREATED", "QUEUED", "RETRYING", "RUNNING"})
KESTRA_STATE_TO_RUN_STATUS = {
    **{state: RunStatus.RUNNING for state in KESTRA_RUNNING_STATES},
    "PAUSED": RunStatus.WAITING_APPROVAL,
    "SUCCESS": RunStatus.FINISHED,
    "WARNING": RunStatus.FINISHED,
    "FAILED": RunStatus.FAILED,
    "KILLED": RunStatus.FAILED,
    "CANCELLED": RunStatus.CANCELLED,
}
TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.FINISHED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class WorkflowService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.drafts = DraftService(session)
        self.settings = get_settings()

    def start(
        self, *, actor: Actor, dry_run: bool | None = None, job_id: str | None = None
    ) -> RunDTO:
        run = WorkflowRun(
            status=RunStatus.RUNNING,
            active_slot=1,
            triggered_by_id=actor.id if actor.type == "user" else None,
            counts={},
            active_job_ids=[job_id] if job_id else [],
            errors=[],
            dry_run=self.settings.email_dry_run if dry_run is None else dry_run,
        )
        self.session.add(run)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise RunAlreadyActiveError() from exc

        self.audit.record(
            actor=actor,
            action="workflow.started",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"dry_run": run.dry_run},
            run_id=run.id,
        )
        self.session.commit()
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def set_kestra_execution_id(self, run_id: str, execution_id: str) -> RunDTO:
        run = self._get_model(run_id)
        if run.kestra_execution_id == execution_id:
            return RunDTO.model_validate(run)

        run.kestra_execution_id = execution_id
        run.last_heartbeat_at = datetime.now(UTC)
        self.audit.record(
            actor=Actor.system("orchestrator"),
            action="workflow.kestra_started",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"kestra_execution_id": execution_id},
            run_id=run.id,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValidationFailedError(
                "This Kestra execution is already linked to another workflow run"
            ) from exc
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def sync_kestra_state(self, run_id: str, kestra_state: str) -> RunDTO:
        """Reconcile the local run status with Kestra's authoritative state."""
        run = self._get_model(run_id)
        normalized = (kestra_state or "").strip().upper()
        target = KESTRA_STATE_TO_RUN_STATUS.get(normalized)
        if target is None or run.status in TERMINAL_RUN_STATUSES:
            return RunDTO.model_validate(run)

        if target == run.status:
            run.last_heartbeat_at = datetime.now(UTC)
            self.session.commit()
            return RunDTO.model_validate(run)

        if target == RunStatus.WAITING_APPROVAL:
            return self.set_waiting_approval(run.id)
        if target == RunStatus.RUNNING:
            # A paused run can only move back to running after a resume.
            return self.mark_running(run.id)
        if target == RunStatus.FINISHED:
            return self.finish(run.id)
        if target == RunStatus.FAILED:
            return self.fail(
                run.id, error=f"Kestra execution ended in state {normalized}"
            )
        if target == RunStatus.CANCELLED:
            return self.cancel(run.id, actor=Actor.system("kestra"))

        return RunDTO.model_validate(run)

    def get(self, run_id: str) -> RunDTO:
        run = self.session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("Workflow run not found")
        return RunDTO.model_validate(run)

    def active_run(self) -> RunDTO | None:
        run = self.session.scalar(
            select(WorkflowRun).where(WorkflowRun.status.in_(list(ACTIVE_RUN_STATUSES)))
        )
        return RunDTO.model_validate(run) if run else None

    def status(self, run_id: str) -> dict:
        run = self._get_model(run_id)
        drafts, _total = self.drafts.list(run_id=run_id, page_size=500)
        open_drafts = [draft for draft in drafts if draft.sent_at is None]
        approved = [draft for draft in open_drafts if draft.approved]
        counts = run.counts or {}
        stage = run.stage or self._infer_stage(run, counts)

        hr_done = bool(counts.get("fetched")) or stage in {
            "forward_to_hr",
            "screen_candidates",
            "draft_decisions",
            "send_decisions",
            "ceo_report",
            "finished",
        }
        manager_done = bool(counts.get("scored")) or stage in {
            "draft_decisions",
            "send_decisions",
            "ceo_report",
            "finished",
        }
        report_done = stage in {"ceo_report", "finished"}

        return {
            "run_id": run.id,
            "status": run.status,
            "stage": stage,
            "dry_run": run.dry_run,
            "kestra_execution_id": run.kestra_execution_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "counts": counts,
            "agents": {
                "hr": {
                    "state": "done" if hr_done else "pending",
                    "detail": (
                        f"{counts.get('created', 0)} candidate(s) created, "
                        f"{counts.get('forwarded', 0)} forwarded"
                    ),
                },
                "hr_manager": {
                    "state": "done" if manager_done else "pending",
                    "detail": (
                        f"{counts.get('shortlisted', 0)} shortlisted, "
                        f"{counts.get('not_shortlisted', 0)} not shortlisted"
                    ),
                },
                "ceo_report": {
                    "state": "done" if report_done else "pending",
                    "detail": (
                        "report sent"
                        if report_done
                        else "waiting for approval"
                        if run.status == RunStatus.WAITING_APPROVAL
                        else "pending"
                    ),
                },
            },
            "pending_approval": {
                "drafts": len(open_drafts),
                "approved": len(approved),
            },
            "approved_draft_count": len(approved),
            "errors": run.errors or [],
        }

    @staticmethod
    def _infer_stage(run: WorkflowRun, counts: dict) -> str:
        if run.status == RunStatus.FINISHED:
            return "finished"
        if run.status == RunStatus.WAITING_APPROVAL:
            return "draft_decisions"
        for key, stage in (
            ("emailed", "ceo_report"),
            ("sent", "send_decisions"),
            ("drafts", "draft_decisions"),
            ("scored", "screen_candidates"),
            ("forwarded", "forward_to_hr"),
            ("fetched", "read_emails"),
        ):
            if counts.get(key):
                return stage
        return "started"

    def set_waiting_approval(self, run_id: str, *, counts: dict | None = None) -> RunDTO:
        run = self._get_model(run_id)
        run.status = RunStatus.WAITING_APPROVAL
        run.stage = "draft_decisions"
        run.last_heartbeat_at = datetime.now(UTC)
        if counts is not None:
            run.counts = counts
        self.audit.record(
            actor=Actor.system("orchestrator"),
            action="workflow.waiting_approval",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"status": run.status},
            run_id=run.id,
        )
        self.session.commit()
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def approve(self, run_id: str, *, actor: Actor) -> int:
        run = self._get_model(run_id)
        if run.approved_at is None and run.status != RunStatus.WAITING_APPROVAL:
            raise ValidationFailedError(
                f"Run is not waiting for approval (status: {run.status})"
            )
        approved = self.drafts.list_approved(run_id=run_id)
        if not approved:
            # R6 still applies to a repeated approval after a draft was revoked.
            raise ApprovalRequiredError("Approve at least one draft before resuming (R6)")
        if run.approved_at is not None:
            # Approval is idempotent: a double click must not resume twice.
            return 0

        run.approved_at = datetime.now(UTC)
        run.approved_by_id = actor.id if actor.type == "user" else None
        self.audit.record(
            actor=actor,
            action="workflow.approved",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"approved_drafts": len(approved)},
            run_id=run.id,
        )
        self.session.commit()
        return len(approved)

    def mark_running(self, run_id: str) -> RunDTO:
        run = self._get_model(run_id)
        run.status = RunStatus.RUNNING
        run.active_slot = 1
        run.last_heartbeat_at = datetime.now(UTC)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise RunAlreadyActiveError() from exc
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def record_progress(
        self,
        run_id: str,
        *,
        counts: dict | None = None,
        errors: list[dict] | None = None,
        stage: str | None = None,
    ) -> None:
        run = self._get_model(run_id)
        if counts is not None:
            run.counts = counts
        if errors is not None:
            run.errors = errors[-200:]
        if stage is not None:
            run.stage = stage
        run.last_heartbeat_at = datetime.now(UTC)
        self.session.commit()

    def finish(self, run_id: str, *, counts: dict | None = None) -> RunDTO:
        run = self._get_model(run_id)
        run.status = RunStatus.FINISHED
        run.stage = "finished"
        run.active_slot = None
        run.finished_at = datetime.now(UTC)
        run.last_heartbeat_at = run.finished_at
        if counts is not None:
            run.counts = counts
        self.audit.record(
            actor=Actor.system("orchestrator"),
            action="workflow.finished",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"status": run.status, "counts": run.counts},
            run_id=run.id,
        )
        self.session.commit()
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def fail(self, run_id: str, *, error: str) -> RunDTO:
        run = self._get_model(run_id)
        run.status = RunStatus.FAILED
        run.stage = "failed"
        run.active_slot = None
        run.finished_at = datetime.now(UTC)
        run.error_message = error[:2000]
        self.audit.record(
            actor=Actor.system("orchestrator"),
            action="workflow.failed",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"status": run.status, "error": run.error_message},
            run_id=run.id,
        )
        self.session.commit()
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def cancel(self, run_id: str, *, actor: Actor) -> RunDTO:
        run = self._get_model(run_id)
        if run.status not in ACTIVE_RUN_STATUSES:
            raise ValidationFailedError("Only an active run can be cancelled")
        run.status = RunStatus.CANCELLED
        run.active_slot = None
        run.finished_at = datetime.now(UTC)
        self.audit.record(
            actor=actor,
            action="workflow.cancelled",
            entity_type="workflow_run",
            entity_id=run.id,
            new_value={"status": run.status},
            run_id=run.id,
        )
        self.session.commit()
        self.session.refresh(run)
        return RunDTO.model_validate(run)

    def heartbeat(self, run_id: str) -> None:
        run = self._get_model(run_id)
        run.last_heartbeat_at = datetime.now(UTC)
        self.session.commit()

    def add_error(self, run_id: str, *, node: str, error: str, item: str | None = None) -> None:
        run = self._get_model(run_id)
        errors = list(run.errors or [])
        errors.append(
            {
                "node": node,
                "item": item,
                "error": error[:500],
                "at": datetime.now(UTC).isoformat(),
            }
        )
        run.errors = errors
        self.session.commit()

    def _get_model(self, run_id: str) -> WorkflowRun:
        run = self.session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("Workflow run not found")
        return run
