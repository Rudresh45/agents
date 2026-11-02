"""Alerting with a one-hour dedupe window (spec section 14)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import structlog

from app.core.config import Settings, get_settings
from app.utils.mailer import Mailer, OutboundEmail

logger = structlog.get_logger(__name__)

DEDUPE_WINDOW_SECONDS = 3600


class AlertService:
    _lock = threading.Lock()
    _last_sent: dict[str, float] = {}

    def __init__(
        self,
        mailer: Mailer | None = None,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self.mailer = mailer or Mailer(self.settings)
        self.clock = clock

    def _should_send(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < DEDUPE_WINDOW_SECONDS:
                return False
            self._last_sent[key] = now
            return True

    def send(self, kind: str, subject: str, detail: str, *, dedupe_key: str | None = None) -> bool:
        key = dedupe_key or kind
        if not self._should_send(key):
            logger.info("alert_deduplicated", kind=kind)
            return False

        logger.warning("alert", kind=kind, subject=subject)
        if not self.settings.alert_email:
            return True

        try:
            self.mailer.send(
                OutboundEmail(
                    to=[self.settings.alert_email],
                    subject=f"[HR Agent] {subject}",
                    body_text=detail,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # An alert failure must never break the workflow.
            logger.error("alert_send_failed", kind=kind, error=str(exc)[:200])
        return True

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._last_sent.clear()
