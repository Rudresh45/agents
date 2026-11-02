from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings


class HealthService:
    """Per-component health. IMAP/SMTP/LLM probes are wired in Phase 2/6."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def check_database(self) -> dict:
        started = time.perf_counter()
        try:
            self.session.execute(text("SELECT 1"))
            return {
                "status": "ok",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": str(exc)}

    def check_imap(self) -> dict:
        configured = bool(self.settings.imap_user and self.settings.imap_password)
        return {"status": "not_checked" if configured else "not_configured"}

    def check_smtp(self) -> dict:
        configured = bool(self.settings.smtp_user and self.settings.smtp_password)
        if self.settings.email_dry_run:
            return {"status": "dry_run"}
        return {"status": "not_checked" if configured else "not_configured"}

    def check_llm(self) -> dict:
        if not self.settings.gemini_api_key:
            return {"status": "not_configured"}
        return {"status": "not_checked", "model": self.settings.gemini_model}

    def check_all(self) -> dict:
        checks = {
            "database": self.check_database(),
            "imap": self.check_imap(),
            "smtp": self.check_smtp(),
            "llm": self.check_llm(),
        }
        statuses = {item["status"] for item in checks.values()}
        if checks["database"]["status"] == "error":
            overall = "unhealthy"
        elif statuses & {"error", "not_checked"}:
            overall = "degraded"
        else:
            overall = "healthy"
        return {"status": overall, "checks": checks}
