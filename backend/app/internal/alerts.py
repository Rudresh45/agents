"""Kestra-facing alert adapter.

Kestra error handlers call this endpoint so a failed flow reaches the same
deduplicated alert channel as the Python workflow.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.internal.schemas import AlertIn, AlertOut
from app.utils.alerts import AlertService


def run_alert(payload: AlertIn) -> AlertOut:
    settings = get_settings()
    service = AlertService(settings=settings)
    sent = service.send(
        payload.kind,
        payload.subject,
        payload.detail,
        dedupe_key=payload.dedupe_key,
    )
    return AlertOut(sent=sent)
