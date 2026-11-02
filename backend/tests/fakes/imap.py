from __future__ import annotations

from pathlib import Path

from app.utils.imap_client import AttachmentMeta, InboundEmail


class FakeImapClient:
    """Feeds pre-built InboundEmail objects and records side effects."""

    def __init__(
        self,
        emails: list[InboundEmail] | None = None,
        *,
        attachment_bytes: bytes = b"%PDF-1.4 fake resume",
    ) -> None:
        self.emails = list(emails or [])
        self.attachment_bytes = attachment_bytes
        self.marked: list[str] = []
        self.downloaded: list[tuple[str, int, Path]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def __enter__(self) -> FakeImapClient:
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]:
        return self.emails[:limit]

    def download_attachment(self, uid: str, meta: AttachmentMeta, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.attachment_bytes)
        self.downloaded.append((uid, meta.part_index, dest))
        return dest

    def mark_processed(self, uid: str) -> None:
        self.marked.append(uid)
