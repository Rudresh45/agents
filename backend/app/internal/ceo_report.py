"""Kestra-facing adapters for the CEO report agent.

The report is assembled and mailed by the backend. Kestra only supplies the
validated CEO summaries produced by its AI tasks.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from app.agents.ceo_report_agent.report import (
    ReportSummaryInput,
    apply_report,
    prepare_report,
)
from app.agents.contracts import AgentContext, Services
from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError, StepLockedError
from app.internal.schemas import (
    ReportApplyIn,
    ReportApplyOut,
    ReportPrepareIn,
    ReportPrepareItemOut,
    ReportPrepareOut,
)
from app.schemas.domain import Actor
from app.utils.imap_client import ImapClient
from app.utils.llm_factory import get_llm
from app.utils.mailer import Mailer
from app.utils.step_lock import StepLock, StepLockBusyError

logger = structlog.get_logger(__name__)

AGENT = Actor.agent("ceo_report_agent")
PREPARE_STEP = "ceo_report.prepare"
APPLY_STEP = "ceo_report.apply"


def _resolve_run_id(services: Services, requested: str | None) -> str | None:
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
    mailer: Mailer,
) -> AgentContext:
    return AgentContext.build(
        session=session,
        llm=get_llm(),
        mailer=mailer,
        imap=ImapClient(settings),
        settings=settings,
        actor=AGENT,
        run_id=run_id,
        dry_run=settings.email_dry_run,
    )


def run_report_prepare(
    session: Session, payload: ReportPrepareIn, *, mailer: Mailer
) -> ReportPrepareOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", PREPARE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            preparation = prepare_report(
                _context(session, settings, run_id=run_id, mailer=mailer),
                job_ids=payload.job_ids,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Report prepare is already running") from exc

    if preparation.stopped:
        raise ExternalServiceError(
            "The CEO report is blocked until every approved draft is sent",
            details={"counts": preparation.counts, "errors": preparation.errors},
        )
    if preparation.errors:
        raise ExternalServiceError(
            "Some CEO summary inputs could not be prepared",
            details={"counts": preparation.counts, "errors": preparation.errors},
        )

    logger.info("report_prepare", **preparation.counts)
    return ReportPrepareOut(
        items=[
            ReportPrepareItemOut(
                candidate_id=item.candidate_id,
                system_prompt=item.system_prompt,
                user_prompt=item.user_prompt,
                prompt_version=item.prompt_version,
                response_schema=item.response_schema,
            )
            for item in preparation.items
        ],
        job_ids=preparation.job_ids,
        counts=preparation.counts,
        skipped=preparation.skipped,
        errors=preparation.errors,
        stopped=preparation.stopped,
        has_work=preparation.has_work,
    )


def run_report_apply(
    session: Session, payload: ReportApplyIn, *, mailer: Mailer
) -> ReportApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", APPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            application = apply_report(
                _context(session, settings, run_id=run_id, mailer=mailer),
                summaries=[
                    ReportSummaryInput(
                        candidate_id=item.candidate_id,
                        result=item.result,
                        model_name=item.model_name,
                        prompt_version=item.prompt_version,
                    )
                    for item in payload.items
                ],
                job_ids=payload.job_ids,
                send=payload.send,
                idempotency_key=payload.idempotency_key,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Report apply is already running") from exc

    if application.errors:
        raise ExternalServiceError(
            "The CEO report could not be completed",
            details={"counts": application.counts, "errors": application.errors},
        )

    logger.info("report_apply", **application.counts)
    return ReportApplyOut(
        counts=application.counts,
        skipped=application.skipped,
        errors=application.errors,
        report_id=application.report_id,
    )
