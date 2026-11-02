from __future__ import annotations

import pytest
from app.core.config import Settings
from app.utils.alerts import AlertService
from tests.fakes.mailer import FakeMailer


@pytest.fixture(autouse=True)
def _reset_alerts():
    AlertService.reset()
    yield
    AlertService.reset()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _service(clock: _Clock, alert_email: str = "alerts@example.com"):
    mailer = FakeMailer(Settings(alert_email=alert_email, email_dry_run=True))
    service = AlertService(
        mailer=mailer, settings=Settings(alert_email=alert_email), clock=clock
    )
    return service, mailer


def test_alert_is_sent_once_per_window():
    clock = _Clock()
    service, mailer = _service(clock)

    assert service.send("imap_failed", "Inbox down", "detail") is True
    assert service.send("imap_failed", "Inbox down", "detail") is False
    assert len(mailer.outbox) == 1


def test_alert_can_be_sent_again_after_window():
    clock = _Clock()
    service, mailer = _service(clock)
    service.send("imap_failed", "Inbox down", "detail")
    clock.advance(3601)
    assert service.send("imap_failed", "Inbox down", "detail") is True
    assert len(mailer.outbox) == 2


def test_different_kinds_have_separate_dedupe_keys():
    clock = _Clock()
    service, mailer = _service(clock)
    service.send("imap_failed", "Inbox down", "detail")
    service.send("llm_failed", "LLM down", "detail")
    assert len(mailer.outbox) == 2


def test_no_alert_email_still_reports_sent():
    clock = _Clock()
    service, mailer = _service(clock, alert_email="")
    assert service.send("run_stuck", "Run stuck", "detail") is True
    assert mailer.outbox == []


def test_alert_failure_does_not_raise():
    clock = _Clock()
    service, _ = _service(clock)
    service.mailer.send = lambda _email: (_ for _ in ()).throw(RuntimeError("smtp down"))
    assert service.send("smtp_failed", "Mail down", "detail") is True
