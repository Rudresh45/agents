"""Optional poller that starts a run when the mailbox has not been checked recently.

Off by default (`SCHEDULER_ENABLED=false`). It is intentionally dependency-free
so a development machine does not need APScheduler; a production deployment can
replace this loop with a worker without touching the graph or agents.
"""

from __future__ import annotations

import threading

import structlog

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.schemas.domain import Actor
from app.services.workflow_service import WorkflowService

logger = structlog.get_logger(__name__)


class Scheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory=SessionLocal,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="hr-scheduler", daemon=True
        )
        self._thread.start()
        logger.info(
            "scheduler_started", interval_minutes=self.settings.poll_interval_minutes
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        interval = max(1, self.settings.poll_interval_minutes) * 60
        while not self._stop.wait(interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the loop
                logger.exception("scheduler_tick_failed")

    def tick(self) -> str | None:
        """Start a run if the slot is free. Returns the new run id, if any."""
        with self.session_factory() as session:
            workflows = WorkflowService(session)
            if workflows.active_run() is not None:
                return None
            run = workflows.start(actor=Actor.system("scheduler"))
        logger.info("scheduler_started_run", run_id=run.id)
        return run.id
