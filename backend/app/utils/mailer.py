"""SMTP mailer with dry-run mode, retries and Message-ID tracking."""

from __future__ import annotations

import mimetypes
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError, ValidationFailedError
from app.utils.retry import retry_call

logger = structlog.get_logger(__name__)


class OutboundEmail(BaseModel):
    to: list[str] = Field(min_length=1)
    subject: str
    body_text: str
    attachments: list[str] = Field(default_factory=list)
    reply_to: str | None = None


class SendResult(BaseModel):
    message_id: str
    dry_run: bool
    sent_at: datetime


class Mailer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _build_message(self, email: OutboundEmail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.settings.smtp_from or self.settings.smtp_user
        message["To"] = ", ".join(email.to)
        message["Subject"] = email.subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain="hr-agent.local")
        reply_to = email.reply_to or self.settings.smtp_reply_to
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(email.body_text)

        for attachment in email.attachments:
            path = Path(attachment)
            if not path.is_file():
                raise ValidationFailedError(
                    "Attachment not found", details={"path": path.name}
                )
            mime, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            message.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
            )
        return message

    def _connect(self) -> smtplib.SMTP:
        settings = self.settings
        if settings.smtp_port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
        else:
            client = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
            client.ehlo()
            client.starttls()
            client.ehlo()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        return client

    def send(self, email: OutboundEmail) -> SendResult:
        message = self._build_message(email)
        message_id = str(message["Message-ID"])

        if self.settings.email_dry_run:
            logger.info(
                "email_dry_run",
                to=email.to,
                subject=email.subject,
                attachments=len(email.attachments),
                message_id=message_id,
            )
            return SendResult(message_id=message_id, dry_run=True, sent_at=datetime.now(UTC))

        def _send_once() -> None:
            with self._connect() as client:
                client.send_message(message)

        try:
            retry_call(_send_once, attempts=3, base_delay=1.0, max_delay=8.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("smtp_failed", error=str(exc)[:300], to=email.to)
            raise ExternalServiceError(
                "Could not send the email", code="smtp_failed"
            ) from exc

        logger.info("email_sent", to=email.to, subject=email.subject, message_id=message_id)
        return SendResult(message_id=message_id, dry_run=False, sent_at=datetime.now(UTC))
