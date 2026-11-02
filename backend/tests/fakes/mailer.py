from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings
from app.utils.mailer import Mailer, OutboundEmail, SendResult


class FakeMailer(Mailer):
    """Captures outgoing mail instead of sending it."""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings or Settings(email_dry_run=True))
        self.outbox: list[tuple[OutboundEmail, SendResult]] = []

    def send(self, email: OutboundEmail) -> SendResult:
        result = SendResult(
            message_id=f"<fake-{len(self.outbox) + 1}@test.local>",
            dry_run=True,
            sent_at=datetime.now(UTC),
        )
        self.outbox.append((email, result))
        return result

    def subjects(self) -> list[str]:
        return [email.subject for email, _ in self.outbox]
