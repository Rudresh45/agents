"""IMAP inbox reader. Read-only fetch, explicit mark-as-processed."""

from __future__ import annotations

import email
import imaplib
import re
from contextlib import suppress
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger(__name__)

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_SUFFIXES = {".pdf", ".docx"}


class AttachmentMeta(BaseModel):
    part_index: int
    filename: str
    content_type: str
    size: int


class InboundEmail(BaseModel):
    uid: str
    message_id: str = ""
    from_address: str = ""
    to_address: str = ""
    subject: str = ""
    body_text: str = ""
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    received_at: datetime | None = None
    attachments: list[AttachmentMeta] = Field(default_factory=list)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:120] or "attachment"


class ImapClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        settings = self.settings
        if settings.email_dry_run or not settings.imap_password:
            logger.info("imap_dry_run_connected", mailbox=settings.imap_mailbox)
            return
        try:
            client = imaplib.IMAP4_SSL(
                settings.imap_host,
                settings.imap_port,
                timeout=settings.imap_timeout_seconds,
            )
            client.login(settings.imap_user, settings.imap_password)
            client.select(settings.imap_mailbox, readonly=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("imap_connect_failed", error=str(exc)[:300])
            raise ExternalServiceError(
                "Could not connect to the inbox", code="imap_failed"
            ) from exc
        self._client = client

    def close(self) -> None:
        if self._client is None:
            return
        with suppress(Exception):
            self._client.close()
        with suppress(Exception):
            self._client.logout()
        self._client = None

    def __enter__(self) -> ImapClient:
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _require(self) -> imaplib.IMAP4_SSL:
        if self._client is None:
            raise ExternalServiceError("IMAP client is not connected", code="imap_failed")
        return self._client

    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]:
        if self.settings.email_dry_run or not self.settings.imap_password:
            return []
        client = self._require()
        try:
            status, data = client.uid("SEARCH", None, "UNSEEN")
            if status != "OK":
                raise ExternalServiceError("IMAP search failed", code="imap_failed")
            uids = data[0].split()[-limit:] if data and data[0] else []
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExternalServiceError("IMAP search failed", code="imap_failed") from exc

        messages: list[InboundEmail] = []
        for raw_uid in uids:
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
            try:
                message = self._fetch_one(uid)
                if message is not None:
                    messages.append(message)
            except Exception as exc:  # noqa: BLE001
                # One broken message must not stop the batch.
                logger.warning("imap_message_failed", uid=uid, error=str(exc)[:200])
        return messages

    def _fetch_one(self, uid: str) -> InboundEmail | None:
        client = self._require()
        status, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        raw = data[0][1]
        if not isinstance(raw, bytes):
            return None
        parsed = email.message_from_bytes(raw)

        body_text = ""
        attachments: list[AttachmentMeta] = []
        parts = [part for part in parsed.walk() if not part.is_multipart()]
        for index, part in enumerate(parts):
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()
            if "attachment" in disposition and filename:
                payload = part.get_payload(decode=True) or b""
                safe = _safe_filename(_decode_header(filename))
                suffix = Path(safe).suffix.lower()
                if suffix in ALLOWED_ATTACHMENT_SUFFIXES and len(payload) <= MAX_ATTACHMENT_BYTES:
                    attachments.append(
                        AttachmentMeta(
                            part_index=index,
                            filename=safe,
                            content_type=content_type,
                            size=len(payload),
                        )
                    )
            elif content_type == "text/plain" and not body_text:
                body_text = _part_text(part)

        received_at = None
        if parsed.get("Date"):
            try:
                received_at = parsedate_to_datetime(parsed["Date"])
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                received_at = None

        references = (parsed.get("References") or "").split()

        return InboundEmail(
            uid=uid,
            message_id=_decode_header(parsed.get("Message-ID")),
            from_address=_decode_header(parsed.get("From")),
            to_address=_decode_header(parsed.get("To")),
            subject=_decode_header(parsed.get("Subject")),
            body_text=body_text.strip(),
            in_reply_to=_decode_header(parsed.get("In-Reply-To")) or None,
            references=[ref.strip("<>") for ref in references],
            received_at=received_at,
            attachments=attachments,
        )

    def download_attachment(self, uid: str, meta: AttachmentMeta, dest: Path) -> Path:
        client = self._require()
        status, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise ExternalServiceError("Could not fetch the message", code="imap_failed")
        parsed = email.message_from_bytes(data[0][1])
        parts = [part for part in parsed.walk() if not part.is_multipart()]
        if meta.part_index >= len(parts):
            raise ExternalServiceError("Attachment part not found", code="imap_failed")
        payload = parts[meta.part_index].get_payload(decode=True)
        if payload is None:
            raise ExternalServiceError("Attachment has no content", code="imap_failed")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest

    def mark_processed(self, uid: str) -> None:
        client = self._require()
        try:
            client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("imap_mark_failed", uid=uid, error=str(exc)[:200])
