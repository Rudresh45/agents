from __future__ import annotations

from fastapi import APIRouter

from app.internal.alerts import run_alert
from app.internal.ceo_report import run_report_apply, run_report_prepare
from app.internal.deps import DbSession, ImapDep, InternalService, MailerDep
from app.internal.hr_agent import (
    run_draft_apply,
    run_draft_prepare,
    run_forward_apply,
    run_forward_prepare,
    run_inbox_acknowledge,
    run_inbox_apply,
    run_inbox_prepare,
    run_inbox_reply,
    run_send_approved,
)
from app.internal.hr_manager import run_screen_apply, run_screen_prepare
from app.internal.schemas import (
    AlertIn,
    AlertOut,
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
    InternalPingOut,
    ReportApplyIn,
    ReportApplyOut,
    ReportPrepareIn,
    ReportPrepareOut,
    ScreenApplyIn,
    ScreenApplyOut,
    ScreenPrepareIn,
    ScreenPrepareOut,
    SendApprovedIn,
    SendApprovedOut,
)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get(
    "/ping",
    response_model=InternalPingOut,
    summary="Internal service-token probe",
)
def ping(_service: InternalService) -> InternalPingOut:
    return InternalPingOut()


@router.post(
    "/hr-manager/screen/prepare",
    response_model=ScreenPrepareOut,
    summary="Prepare sanitised HR manager screening input",
)
def screen_prepare(
    payload: ScreenPrepareIn,
    session: DbSession,
    _service: InternalService,
) -> ScreenPrepareOut:
    return run_screen_prepare(session, payload)


@router.post(
    "/hr-manager/screen/apply",
    response_model=ScreenApplyOut,
    summary="Apply validated HR manager screening results",
)
def screen_apply(
    payload: ScreenApplyIn,
    session: DbSession,
    _service: InternalService,
) -> ScreenApplyOut:
    return run_screen_apply(session, payload)


@router.post(
    "/hr-agent/inbox/prepare",
    response_model=InboxPrepareOut,
    summary="Read the inbox and return redacted classification input",
)
def inbox_prepare(
    payload: InboxPrepareIn,
    session: DbSession,
    _service: InternalService,
    imap: ImapDep,
    mailer: MailerDep,
) -> InboxPrepareOut:
    return run_inbox_prepare(session, payload, imap=imap, mailer=mailer)


@router.post(
    "/hr-agent/inbox/apply",
    response_model=InboxApplyOut,
    summary="Apply one validated application classification",
)
def inbox_apply(
    payload: InboxApplyIn,
    session: DbSession,
    _service: InternalService,
    imap: ImapDep,
    mailer: MailerDep,
) -> InboxApplyOut:
    return run_inbox_apply(session, payload, imap=imap, mailer=mailer)


@router.post(
    "/hr-agent/inbox/reply",
    response_model=InboxApplyOut,
    summary="Apply one validated candidate reply classification",
)
def inbox_reply(
    payload: InboxReplyApplyIn,
    session: DbSession,
    _service: InternalService,
    imap: ImapDep,
    mailer: MailerDep,
) -> InboxApplyOut:
    return run_inbox_reply(session, payload, imap=imap, mailer=mailer)


@router.post(
    "/hr-agent/inbox/acknowledge",
    response_model=InboxAcknowledgeOut,
    summary="Create the acknowledgement or resubmission draft once",
)
def inbox_acknowledge(
    payload: InboxAcknowledgeIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> InboxAcknowledgeOut:
    return run_inbox_acknowledge(session, payload, mailer=mailer)


@router.post(
    "/hr-agent/forward/prepare",
    response_model=ForwardPrepareOut,
    summary="Prepare redacted resume summaries for the digest",
)
def forward_prepare(
    payload: ForwardPrepareIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> ForwardPrepareOut:
    return run_forward_prepare(session, payload, mailer=mailer)


@router.post(
    "/hr-agent/forward/apply",
    response_model=ForwardApplyOut,
    summary="Apply candidate summaries and send the digest once",
)
def forward_apply(
    payload: ForwardApplyIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> ForwardApplyOut:
    return run_forward_apply(session, payload, mailer=mailer)


@router.post(
    "/hr-agent/draft/prepare",
    response_model=DraftPrepareOut,
    summary="Prepare redacted decision-email input",
)
def draft_prepare(
    payload: DraftPrepareIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> DraftPrepareOut:
    return run_draft_prepare(session, payload, mailer=mailer)


@router.post(
    "/hr-agent/draft/apply",
    response_model=DraftApplyOut,
    summary="Save validated decision-email drafts",
)
def draft_apply(
    payload: DraftApplyIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> DraftApplyOut:
    return run_draft_apply(session, payload, mailer=mailer)


@router.post(
    "/hr-agent/send/approved",
    response_model=SendApprovedOut,
    summary="Send only HR-approved drafts",
)
def send_approved(
    payload: SendApprovedIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> SendApprovedOut:
    return run_send_approved(session, payload, mailer=mailer)


@router.post(
    "/ceo-report/prepare",
    response_model=ReportPrepareOut,
    summary="Prepare redacted CEO summary input",
)
def report_prepare(
    payload: ReportPrepareIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> ReportPrepareOut:
    return run_report_prepare(session, payload, mailer=mailer)


@router.post(
    "/ceo-report/apply",
    response_model=ReportApplyOut,
    summary="Build and send the CEO report once",
)
def report_apply(
    payload: ReportApplyIn,
    session: DbSession,
    _service: InternalService,
    mailer: MailerDep,
) -> ReportApplyOut:
    return run_report_apply(session, payload, mailer=mailer)


@router.post(
    "/alerts",
    response_model=AlertOut,
    summary="Send a deduplicated workflow alert",
)
def alerts(
    payload: AlertIn,
    _service: InternalService,
) -> AlertOut:
    return run_alert(payload)
