from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import (
    DbSession,
    HrOrManager,
    HrUser,
    KestraDep,
    SettingsDep,
    actor_for,
)
from app.core.exceptions import ConflictError, RunAlreadyActiveError, ValidationFailedError
from app.schemas.api import (
    WorkflowApproveRequest,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowStatusOut,
)
from app.services.draft_service import DraftService
from app.services.job_service import JobService
from app.services.workflow_service import TERMINAL_RUN_STATUSES, WorkflowService

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowRunResponse,
    summary="Start a workflow run",
    responses={409: {"description": "A run is already active"}},
)
def start_run(
    payload: WorkflowRunRequest,
    user: HrOrManager,
    db: DbSession,
    settings: SettingsDep,
    kestra: KestraDep,
) -> WorkflowRunResponse:
    job_id = payload.job_id
    if not job_id:
        raise ValidationFailedError("job_id is required")
    # Validate before cancelling an active run or creating a new one.
    JobService(db).get(job_id)

    service = WorkflowService(db)
    active = service.active_run()
    if active is not None:
        if not payload.force:
            raise RunAlreadyActiveError()
        if active.kestra_execution_id:
            kestra.kill_execution(active.kestra_execution_id)
        service.cancel(active.id, actor=actor_for(user))

    run = service.start(
        actor=actor_for(user),
        job_id=job_id,
    )

    try:
        execution = kestra.start_screening(
            job_id, run_id=run.id, correlation_id=run.id
        )
        run = service.set_kestra_execution_id(run.id, execution.id)
    except Exception as exc:  # noqa: BLE001 - always free the active slot
        service.fail(run.id, error=f"Kestra start failed: {type(exc).__name__}")
        raise
    return WorkflowRunResponse(
        run_id=run.id,
        status=run.status,
        kestra_execution_id=run.kestra_execution_id,
    )


@router.get(
    "/status/{run_id}",
    response_model=WorkflowStatusOut,
    summary="Run status, stage, counts and pending approvals",
)
def run_status(
    run_id: str, user: HrOrManager, db: DbSession, kestra: KestraDep
) -> WorkflowStatusOut:
    service = WorkflowService(db)
    run = service.get(run_id)
    kestra_state = None

    if run.kestra_execution_id:
        execution = kestra.get_execution(run.kestra_execution_id)
        kestra_state = execution.state
        service.sync_kestra_state(run.id, execution.state)

    body = service.status(run_id)
    body["kestra_state"] = kestra_state
    return WorkflowStatusOut.model_validate(body)


@router.post(
    "/approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowRunResponse,
    summary="Approve drafts and resume the paused run",
    responses={409: {"description": "No draft is approved"}},
)
def approve_run(
    payload: WorkflowApproveRequest,
    user: HrUser,
    db: DbSession,
    kestra: KestraDep,
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    run = service.get(payload.run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ConflictError(f"Run is not waiting for approval (status: {run.status})")

    if run.kestra_execution_id:
        execution = kestra.get_execution(run.kestra_execution_id)
        kestra_state = execution.state.upper()
        service.sync_kestra_state(run.id, kestra_state)
        run = service.get(run.id)
        if run.status in TERMINAL_RUN_STATUSES:
            raise ConflictError(
                f"Run is not waiting for approval (status: {run.status})"
            )
        if kestra_state != "PAUSED":
            # A repeated approval after a successful resume must not resume twice.
            if run.approved_at is not None:
                return WorkflowRunResponse(
                    run_id=run.id,
                    status=run.status,
                    kestra_execution_id=run.kestra_execution_id,
                )
            raise ConflictError(
                "Kestra execution is not paused for approval "
                f"(state: {kestra_state})"
            )

    drafts = DraftService(db)
    for draft_id in payload.draft_ids or []:
        draft = drafts.get(draft_id)
        if draft.run_id != run.id:
            raise ConflictError("Draft does not belong to this run")
        drafts.approve(draft_id, actor=actor_for(user))

    service.approve(run.id, actor=actor_for(user))

    if run.kestra_execution_id:
        kestra.resume_execution(
            run.kestra_execution_id,
            approved=True,
            approved_by=user.email,
        )
        service.mark_running(run.id)
        return WorkflowRunResponse(
            run_id=run.id,
            status="running",
            kestra_execution_id=run.kestra_execution_id,
        )

    return WorkflowRunResponse(run_id=run.id, status=run.status)


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowRunResponse,
    summary="Cancel a run that is waiting for approval",
)
def cancel_run(
    run_id: str, user: HrUser, db: DbSession, kestra: KestraDep
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    run = service.get(run_id)
    if run.status != "waiting_approval":
        raise ConflictError("Only a run waiting for approval can be cancelled")
    if run.kestra_execution_id:
        kestra.kill_execution(run.kestra_execution_id)
    cancelled = service.cancel(run_id, actor=actor_for(user))
    return WorkflowRunResponse(
        run_id=cancelled.id,
        status=cancelled.status,
        kestra_execution_id=cancelled.kestra_execution_id,
    )
