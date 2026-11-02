"""Kestra-facing adapters for the HR agent.

Routes only validate JSON. These functions take a step lock, build the agent
context, and call the prepare/apply functions. No LLM call happens here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict

import structlog
from sqlalchemy.orm import Session

from app.agents.contracts import AgentContext, Services
from app.agents.hr_agent.draft import (
    DraftInput,
    apply_drafts,
    prepare_drafts,
)
from app.agents.hr_agent.forward import (
    ForwardSummaryInput,
    apply_forward,
    prepare_forward,
)
from app.agents.hr_agent.intake import (
    InboxApplicationInput,
    InboxReplyInput,
    acknowledge_candidate,
    apply_inbox_application,
    apply_inbox_reply,
    prepare_inbox,
)
from app.agents.hr_agent.send import send_approved
from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError, StepLockedError
from app.internal.schemas import (
    DraftApplyIn,
    DraftApplyOut,
    DraftPrepareIn,
    DraftPrepareOut,
    ForwardApplyIn,
    ForwardApplyOut,
    ForwardPrepareIn,
    ForwardPrepareOut,
    InboxAcknowledgeIn,
    InboxAcknowledgeOut,
    InboxApplyIn,
    InboxApplyOut,
    InboxPrepareIn,
    InboxPrepareOut,
    InboxReplyApplyIn,
    SendApprovedIn,
    SendApprovedOut,
)
from app.schemas.domain import Actor
from app.utils.imap_client import ImapClient
from app.utils.llm_factory import get_llm
from app.utils.mailer import Mailer
from app.utils.step_lock import StepLock, StepLockBusyError

logger = structlog.get_logger(__name__)

AGENT = Actor.agent("hr_agent")
INBOX_PREPARE_STEP = "hr_agent.inbox.prepare"
INBOX_APPLY_STEP = "hr_agent.inbox.apply"
INBOX_REPLY_STEP = "hr_agent.inbox.reply"
ACKNOWLEDGE_STEP = "hr_agent.inbox.acknowledge"
FORWARD_PREPARE_STEP = "hr_agent.forward.prepare"
FORWARD_APPLY_STEP = "hr_agent.forward.apply"
DRAFT_PREPARE_STEP = "hr_agent.draft.prepare"
DRAFT_APPLY_STEP = "hr_agent.draft.apply"
SEND_STEP = "hr_agent.send.approved"


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
    imap: ImapClient,
    mailer: Mailer,
) -> AgentContext:
    return AgentContext.build(
        session=session,
        llm=get_llm(),
        mailer=mailer,
        imap=imap,
        settings=settings,
        actor=AGENT,
        run_id=run_id,
        dry_run=settings.email_dry_run,
    )


@contextmanager
def _imap_session(imap: ImapClient) -> Iterator[ImapClient]:
    connected = False
    try:
        imap.connect()
        connected = True
        yield imap
    finally:
        if connected:
            with suppress(Exception):
                imap.close()


def run_inbox_prepare(
    session: Session, payload: InboxPrepareIn, *, imap: ImapClient, mailer: Mailer
) -> InboxPrepareOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", INBOX_PREPARE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            with _imap_session(imap):
                preparation = prepare_inbox(
                    _context(
                        session,
                        settings,
                        run_id=run_id,
                        imap=imap,
                        mailer=mailer,
                    ),
                    limit=payload.limit,
                )
    except StepLockBusyError as exc:
        raise StepLockedError("Inbox prepare is already running") from exc

    logger.info("inbox_prepare", **preparation.counts)
    applications = [
        {
            "email_key": item.email_key,
            "job_id": item.job_id,
            "classification": asdict(item.classification),
            "job_match": asdict(item.job_match) if item.job_match else None,
        }
        for item in preparation.applications
    ]
    return InboxPrepareOut(
        applications=applications,
        alias_applications=[item for item in applications if item["job_match"] is None],
        ai_applications=[item for item in applications if item["job_match"] is not None],
        replies=[
            {"email_key": item.email_key, "reply": asdict(item.reply)}
            for item in preparation.replies
        ],
        counts=preparation.counts,
        skipped=preparation.skipped,
        errors=preparation.errors,
        has_work=preparation.has_work,
    )


def run_inbox_apply(
    session: Session, payload: InboxApplyIn, *, imap: ImapClient, mailer: Mailer
) -> InboxApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", INBOX_APPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            with _imap_session(imap):
                outcome = apply_inbox_application(
                    _context(
                        session,
                        settings,
                        run_id=run_id,
                        imap=imap,
                        mailer=mailer,
                    ),
                    InboxApplicationInput(
                        email_key=payload.email_key,
                        classification=payload.classification,
                        classification_model=payload.classification_model,
                        classification_prompt_version=payload.classification_prompt_version,
                        job_match=payload.job_match,
                        job_match_model=payload.job_match_model,
                        job_match_prompt_version=payload.job_match_prompt_version,
                    ),
                )
    except StepLockBusyError as exc:
        raise StepLockedError("Inbox apply is already running") from exc

    if outcome.errors:
        raise ExternalServiceError(
            "The application could not be applied",
            details={"counts": outcome.counts, "errors": outcome.errors},
        )
    logger.info("inbox_apply", **outcome.counts)
    return InboxApplyOut(
        counts=outcome.counts,
        candidate_id=outcome.candidate_id,
        draft_type=outcome.draft_type,
        skipped=outcome.skipped,
        errors=outcome.errors,
    )


def run_inbox_reply(
    session: Session, payload: InboxReplyApplyIn, *, imap: ImapClient, mailer: Mailer
) -> InboxApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", INBOX_REPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            with _imap_session(imap):
                outcome = apply_inbox_reply(
                    _context(
                        session,
                        settings,
                        run_id=run_id,
                        imap=imap,
                        mailer=mailer,
                    ),
                    InboxReplyInput(
                        email_key=payload.email_key,
                        reply=payload.reply,
                        model_name=payload.model_name,
                        prompt_version=payload.prompt_version,
                    ),
                )
    except StepLockBusyError as exc:
        raise StepLockedError("Inbox reply apply is already running") from exc

    if outcome.errors:
        raise ExternalServiceError(
            "The candidate reply could not be applied",
            details={"counts": outcome.counts, "errors": outcome.errors},
        )
    logger.info("inbox_reply", **outcome.counts)
    return InboxApplyOut(
        counts=outcome.counts,
        candidate_id=outcome.candidate_id,
        skipped=outcome.skipped,
        errors=outcome.errors,
    )


def run_inbox_acknowledge(
    session: Session, payload: InboxAcknowledgeIn, *, mailer: Mailer
) -> InboxAcknowledgeOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", ACKNOWLEDGE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            outcome = acknowledge_candidate(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                candidate_id=payload.candidate_id,
                draft_type=payload.draft_type,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Acknowledge is already running") from exc

    if outcome.errors:
        raise ExternalServiceError(
            "The acknowledgement draft could not be created",
            details={"errors": outcome.errors},
        )
    return InboxAcknowledgeOut(
        created=outcome.created,
        draft_id=outcome.draft_id,
        skipped=outcome.skipped,
        errors=outcome.errors,
    )


def run_forward_prepare(
    session: Session, payload: ForwardPrepareIn, *, mailer: Mailer
) -> ForwardPrepareOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", FORWARD_PREPARE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            preparation = prepare_forward(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                limit=payload.limit,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Forward prepare is already running") from exc

    return ForwardPrepareOut(
        items=[
            {
                "candidate_id": item.candidate_id,
                "job_id": item.job_id,
                "system_prompt": item.system_prompt,
                "user_prompt": item.user_prompt,
                "prompt_version": item.prompt_version,
                "response_schema": item.response_schema,
            }
            for item in preparation.items
        ],
        candidate_ids=preparation.candidate_ids,
        counts=preparation.counts,
        skipped=preparation.skipped,
        errors=preparation.errors,
        has_work=preparation.has_work,
    )


def run_forward_apply(
    session: Session, payload: ForwardApplyIn, *, mailer: Mailer
) -> ForwardApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", FORWARD_APPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            application = apply_forward(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                summaries=[
                    ForwardSummaryInput(
                        candidate_id=item.candidate_id,
                        result=item.result,
                        model_name=item.model_name,
                        prompt_version=item.prompt_version,
                    )
                    for item in payload.items
                ],
                candidate_ids=payload.candidate_ids,
                finalize=payload.finalize,
                idempotency_key=payload.idempotency_key,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Forward apply is already running") from exc

    if payload.finalize and application.errors:
        raise ExternalServiceError(
            "Candidate digest could not be completed",
            details={"counts": application.counts, "errors": application.errors},
        )
    return ForwardApplyOut(
        counts=application.counts,
        skipped=application.skipped,
        errors=application.errors,
    )


def run_draft_prepare(
    session: Session, payload: DraftPrepareIn, *, mailer: Mailer
) -> DraftPrepareOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", DRAFT_PREPARE_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            preparation = prepare_drafts(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                job_id=payload.job_id,
                limit=payload.limit,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Draft prepare is already running") from exc

    return DraftPrepareOut(
        items=[
            {
                "candidate_id": item.candidate_id,
                "job_id": item.job_id,
                "draft_type": item.draft_type,
                "system_prompt": item.system_prompt,
                "user_prompt": item.user_prompt,
                "prompt_version": item.prompt_version,
                "response_schema": item.response_schema,
            }
            for item in preparation.items
        ],
        counts=preparation.counts,
        skipped=preparation.skipped,
        errors=preparation.errors,
        has_work=preparation.has_work,
    )


def run_draft_apply(
    session: Session, payload: DraftApplyIn, *, mailer: Mailer
) -> DraftApplyOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", DRAFT_APPLY_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            application = apply_drafts(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                items=[
                    DraftInput(
                        candidate_id=item.candidate_id,
                        draft_type=item.draft_type,
                        result=item.result,
                        model_name=item.model_name,
                        prompt_version=item.prompt_version,
                    )
                    for item in payload.items
                ],
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Draft apply is already running") from exc

    if application.errors:
        raise ExternalServiceError(
            "One or more decision drafts could not be saved",
            details={"counts": application.counts, "errors": application.errors},
        )
    return DraftApplyOut(
        counts=application.counts,
        skipped=application.skipped,
        errors=application.errors,
    )


def run_send_approved(
    session: Session, payload: SendApprovedIn, *, mailer: Mailer
) -> SendApprovedOut:
    settings = get_settings()
    services = Services.from_session(session)
    try:
        with StepLock(settings.upload_dir / ".locks", SEND_STEP):
            run_id = _resolve_run_id(services, payload.run_id)
            result = send_approved(
                _context(
                    session,
                    settings,
                    run_id=run_id,
                    imap=ImapClient(settings),
                    mailer=mailer,
                ),
                draft_ids=payload.draft_ids,
            )
    except StepLockBusyError as exc:
        raise StepLockedError("Send approved is already running") from exc

    if result.errors:
        raise ExternalServiceError(
            "One or more approved emails could not be sent",
            details={"counts": result.counts, "errors": result.errors},
        )
    return SendApprovedOut(counts=result.counts, errors=result.errors)
