"""Kestra-facing adapter for the HR manager screening step.

The route layer only validates JSON. This module takes the step lock, calls the
HR manager agent's prepare/apply functions, and builds the shortlist workbook
through the existing agent. No LLM call happens here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from app.agents.contracts import AgentContext, Services
from app.agents.hr_manager_agent.screening import (
    ScreeningResultInput,
    apply_screening,
    prepare_screening,
)
from app.agents.hr_manager_agent.shortlist import build_shortlist_file
from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError, StepLockedError
from app.internal.schemas import (
    ScreenApplyIn,
    ScreenApplyOut,
    ScreenPrepareIn,
    ScreenPrepareItemOut,
    ScreenPrepareOut,
)
from app.schemas.domain import Actor
from app.utils.imap_client import ImapClient
from app.utils.llm_factory import get_llm
from app.utils.mailer import Mailer
from app.utils.step_lock import StepLock, StepLockBusyError

logger = structlog.get_logger(__name__)

AGENT = Actor.agent("hr_manager_agent")
PREPARE_STEP = "hr_manager.screen.prepare"
APPLY_STEP = "hr_manager.screen.apply"


def _resolve_run_id(services: Services, requested: str | None) -> str | None:
    """Return a real WorkflowRun id, or None when this is a standalone call."""
    if requested:
        services.workflows.get(requested)
        return requested
    active = services.workflows.active_run()
    return active.id if active else None


def _context(
    session: Session,
    settings: Settings,
    *,
    run_id: str | None,
) -> AgentContext:
    return AgentContext.build(
        session=session,
        llm=get_llm(),
        mailer=Mailer(settings),
        imap=ImapClient(settings),
        settings=settings,
        actor=AGENT,
        run_id=run_id,
        dry_run=settings.email_dry_run,
    )


def run_screen_prepare(session: Session, payload: ScreenPrepareIn) -> ScreenPrepareOut:
    settings = get_settings()
    services = Services.from_session(session)

    try:
        with StepLock(settings.upload_dir / ".locks", PREPARE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            preparation = prepare_screening(
                services,
                job_id=payload.job_id,
                limit=payload.limit,
                actor=AGENT,
                run_id=run_id,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Screening prepare is already running") from exc

    if preparation.errors:
        raise ExternalServiceError(
            "Some screening inputs could not be prepared",
            details={
                "counts": preparation.counts,
                "errors": preparation.errors,
            },
        )

    logger.info("screen_prepare", **preparation.counts)
    return ScreenPrepareOut(
        items=[
            ScreenPrepareItemOut(
                candidate_id=item.candidate_id,
                job_id=item.job_id,
                system_prompt=item.system_prompt,
                user_prompt=item.user_prompt,
                prompt_version=item.prompt_version,
                response_schema=item.response_schema,
            )
            for item in preparation.items
        ],
        counts=preparation.counts,
        skipped=preparation.skipped,
        errors=preparation.errors,
        has_work=bool(preparation.items),
        stopped=preparation.stopped,
    )


def run_screen_apply(session: Session, payload: ScreenApplyIn) -> ScreenApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    shortlist: dict = {}

    try:
        with StepLock(settings.upload_dir / ".locks", APPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            application = apply_screening(
                services,
                results=[
                    ScreeningResultInput(
                        candidate_id=item.candidate_id,
                        result=item.result,
                        model_name=item.model_name,
                        prompt_version=item.prompt_version,
                    )
                    for item in payload.items
                ],
                actor=AGENT,
                run_id=run_id,
            )
            if application.errors:
                raise ExternalServiceError(
                    "Some screening results could not be applied",
                    details={
                        "counts": application.counts,
                        "errors": application.errors,
                    },
                )

            if payload.finalize:
                shortlist_job_id = payload.job_id
                if shortlist_job_id is None and len(application.job_ids) == 1:
                    shortlist_job_id = next(iter(application.job_ids))
                idempotency_key = payload.idempotency_key or (
                    f"run:{run_id}"
                    if run_id
                    else f"job:{payload.job_id or 'all'}:{datetime.now(UTC).date().isoformat()}"
                )
                shortlist_result = build_shortlist_file(
                    _context(
                        session,
                        settings,
                        run_id=run_id,
                    ),
                    job_id=shortlist_job_id,
                    idempotency_key=idempotency_key,
                    email_run_id=run_id,
                )
                shortlist = {
                    "counts": shortlist_result.counts,
                    "errors": shortlist_result.errors,
                }
                if shortlist_result.errors:
                    raise ExternalServiceError(
                        "Shortlist build or email failed",
                        details={
                            "counts": application.counts,
                            "errors": application.errors,
                            "shortlist": shortlist,
                        },
                    )
    except StepLockBusyError as exc:
        raise StepLockedError("Screening apply is already running") from exc

    logger.info("screen_apply", **application.counts)
    return ScreenApplyOut(
        counts=application.counts,
        skipped=application.skipped,
        errors=application.errors,
        shortlist=shortlist,
        finalized=payload.finalize,
    )
