from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings
from app.schemas.domain import Actor
from app.services.audit_service import AuditService
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService
from app.services.email_log_service import EmailLogService
from app.services.job_service import JobService
from app.services.privacy_service import PrivacyService
from app.services.reply_service import ReplyService
from app.services.report_service import ReportService
from app.services.workflow_service import WorkflowService
from app.utils.alerts import AlertService
from app.utils.imap_client import AttachmentMeta, InboundEmail
from app.utils.llm_factory import StructuredLLM
from app.utils.mailer import OutboundEmail, SendResult
from app.utils.storage import Storage


class ImapReader(Protocol):
    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]: ...
    def download_attachment(self, uid: str, meta: AttachmentMeta, dest: Path) -> Path: ...
    def mark_processed(self, uid: str) -> None: ...


class MailSender(Protocol):
    def send(self, email: OutboundEmail) -> SendResult: ...


@dataclass
class Services:
    candidates: CandidateService
    jobs: JobService
    drafts: DraftService
    replies: ReplyService
    emails: EmailLogService
    reports: ReportService
    privacy: PrivacyService
    workflows: WorkflowService
    audit: AuditService

    @classmethod
    def from_session(cls, session) -> Services:  # noqa: ANN001
        return cls(
            audit=AuditService(session),
            candidates=CandidateService(session),
            jobs=JobService(session),
            drafts=DraftService(session),
            replies=ReplyService(session),
            emails=EmailLogService(session),
            reports=ReportService(session),
            privacy=PrivacyService(session),
            workflows=WorkflowService(session),
        )


@dataclass
class AgentContext:
    services: Services
    llm: StructuredLLM
    mailer: MailSender
    imap: ImapReader
    storage: Storage
    alerts: AlertService
    settings: Settings
    actor: Actor
    # A scheduled Kestra intake may run outside a WorkflowRun, so this can be
    # None. Never invent an id: audit rows have a real foreign key.
    run_id: str | None
    dry_run: bool

    @classmethod
    def build(
        cls,
        *,
        session,  # noqa: ANN001
        llm: StructuredLLM,
        mailer: MailSender,
        imap: ImapReader,
        alerts: AlertService | None = None,
        actor: Actor | None = None,
        run_id: str | None = "manual",
        dry_run: bool | None = None,
        settings: Settings | None = None,
    ) -> AgentContext:
        cfg = settings or get_settings()
        return cls(
            services=Services.from_session(session),
            llm=llm,
            mailer=mailer,
            imap=imap,
            storage=Storage(cfg),
            alerts=alerts or AlertService(mailer=mailer, settings=cfg),
            settings=cfg,
            actor=actor or Actor.agent("system"),
            run_id=run_id,
            dry_run=cfg.email_dry_run if dry_run is None else dry_run,
        )


@dataclass
class AgentResult:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    stop: bool = False
    data: dict = field(default_factory=dict)

    def merge(self, other: AgentResult) -> AgentResult:
        for key, value in other.counts.items():
            self.counts[key] = self.counts.get(key, 0) + value
        self.errors.extend(other.errors)
        self.stop = self.stop or other.stop
        self.data.update(other.data)
        return self
