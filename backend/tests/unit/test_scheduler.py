from __future__ import annotations

from contextlib import nullcontext

from app.db.models.workflow_run import WorkflowRun
from app.workers.scheduler import Scheduler
from sqlalchemy.orm import Session


def _scheduler(session: Session, settings) -> Scheduler:
    return Scheduler(
        settings,
        session_factory=lambda: nullcontext(session),
    )


def test_disabled_scheduler_does_not_start_a_thread(settings) -> None:
    scheduler = Scheduler(settings.model_copy(update={"scheduler_enabled": False}))
    scheduler.start()
    assert scheduler._thread is None  # noqa: SLF001


def test_tick_starts_a_run_when_the_slot_is_free(
    session: Session, settings
) -> None:
    scheduler = _scheduler(
        session, settings.model_copy(update={"scheduler_enabled": True})
    )

    run_id = scheduler.tick()

    assert run_id is not None
    run = session.get(WorkflowRun, run_id)
    assert run.status == "running"
    assert run.active_slot == 1


def test_tick_is_a_noop_while_a_run_is_active(
    session: Session, settings
) -> None:
    scheduler = _scheduler(session, settings)
    first = scheduler.tick()
    assert first is not None

    assert scheduler.tick() is None
