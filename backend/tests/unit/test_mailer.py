from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.exceptions import ValidationFailedError
from app.utils.mailer import Mailer, OutboundEmail


def _settings(**overrides) -> Settings:
    return Settings(email_dry_run=True, smtp_from="hr@example.com", **overrides)


def test_dry_run_does_not_connect():
    mailer = Mailer(_settings())
    result = mailer.send(
        OutboundEmail(to=["candidate@example.com"], subject="Hello", body_text="Body")
    )
    assert result.dry_run is True
    assert result.message_id.startswith("<")
    assert result.message_id.endswith(">")


def test_message_has_reply_to_and_attachment(tmp_path: Path):
    attachment = tmp_path / "shortlist.xlsx"
    attachment.write_bytes(b"PK\x03\x04 fake")
    mailer = Mailer(_settings(smtp_reply_to="jobs@example.com"))
    message = mailer._build_message(
        OutboundEmail(
            to=["manager@example.com"],
            subject="Shortlist",
            body_text="See attached.",
            attachments=[str(attachment)],
        )
    )
    assert message["Reply-To"] == "jobs@example.com"
    assert message["To"] == "manager@example.com"
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "shortlist.xlsx"


def test_missing_attachment_is_rejected(tmp_path: Path):
    mailer = Mailer(_settings())
    with pytest.raises(ValidationFailedError):
        mailer.send(
            OutboundEmail(
                to=["a@example.com"],
                subject="x",
                body_text="y",
                attachments=[str(tmp_path / "missing.pdf")],
            )
        )


class _FakeSmtp:
    def __init__(self) -> None:
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def send_message(self, message):
        self.sent.append(message)


def test_real_send_path_uses_smtp(monkeypatch):
    settings = Settings(
        email_dry_run=False,
        smtp_from="hr@example.com",
        smtp_user="hr@example.com",
        smtp_password="secret",
    )
    mailer = Mailer(settings)
    fake = _FakeSmtp()
    monkeypatch.setattr(mailer, "_connect", lambda: fake)

    result = mailer.send(
        OutboundEmail(to=["a@example.com"], subject="Hi", body_text="Body")
    )

    assert result.dry_run is False
    assert len(fake.sent) == 1
    assert fake.sent[0]["Subject"] == "Hi"
