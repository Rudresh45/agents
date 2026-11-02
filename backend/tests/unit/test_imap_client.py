from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from app.core.config import Settings
from app.utils.imap_client import ImapClient


def _raw_email(
    with_attachment: bool = True, subject: str = "Application: Backend Engineer"
) -> bytes:
    message = EmailMessage()
    message["From"] = "Priya Sharma <priya@example.com>"
    message["To"] = "hr@example.com"
    message["Subject"] = subject
    message["Message-ID"] = "<in-1@example.com>"
    message["In-Reply-To"] = "<out-1@hr-agent.local>"
    message["References"] = "<out-1@hr-agent.local>"
    message["Date"] = "Wed, 28 Oct 2026 10:00:00 +0000"
    message.set_content("Please find my resume attached.")
    if with_attachment:
        message.add_attachment(
            b"%PDF-1.4 fake resume",
            maintype="application",
            subtype="pdf",
            filename="Priya_Sharma_Resume.pdf",
        )
    return message.as_bytes()


class _StubImap:
    def __init__(self, raw: bytes, uids: bytes = b"7 8") -> None:
        self.raw = raw
        self.uids = uids
        self.stored: list[tuple] = []

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [self.uids]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {123}", self.raw)]
        if command == "STORE":
            self.stored.append(args)
            return "OK", [b""]
        return "NO", [b""]


def _client(stub: _StubImap) -> ImapClient:
    client = ImapClient(Settings(imap_user="hr@example.com", imap_password="secret"))
    client._client = stub  # type: ignore[assignment]
    return client


def test_fetch_unread_parses_headers_body_and_attachment():
    client = _client(_StubImap(_raw_email()))
    emails = client.fetch_unread()

    assert len(emails) == 2
    email = emails[0]
    assert email.uid == "7"
    assert email.subject == "Application: Backend Engineer"
    assert "priya@example.com" in email.from_address
    assert "resume attached" in email.body_text
    assert email.in_reply_to == "<out-1@hr-agent.local>"
    assert email.references == ["out-1@hr-agent.local"]
    assert email.received_at is not None
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "Priya_Sharma_Resume.pdf"
    assert email.attachments[0].content_type == "application/pdf"


def test_fetch_unread_respects_limit():
    client = _client(_StubImap(_raw_email()))
    assert len(client.fetch_unread(limit=1)) == 1


def test_fetch_unread_handles_empty_inbox():
    client = _client(_StubImap(_raw_email(), uids=b""))
    assert client.fetch_unread() == []


def test_non_pdf_attachments_are_ignored():
    message = EmailMessage()
    message["From"] = "a@example.com"
    message["Subject"] = "hi"
    message.set_content("body")
    message.add_attachment(b"PK\x03\x04", maintype="application", subtype="zip", filename="x.zip")
    client = _client(_StubImap(message.as_bytes()))
    assert client.fetch_unread()[0].attachments == []


def test_download_attachment_writes_file(tmp_path: Path):
    client = _client(_StubImap(_raw_email()))
    email = client.fetch_unread()[0]
    dest = tmp_path / "resume.pdf"
    result = client.download_attachment(email.uid, email.attachments[0], dest)
    assert result == dest
    assert dest.read_bytes().startswith(b"%PDF")


def test_mark_processed_sets_seen_flag():
    stub = _StubImap(_raw_email())
    client = _client(stub)
    client.mark_processed("7")
    assert stub.stored
