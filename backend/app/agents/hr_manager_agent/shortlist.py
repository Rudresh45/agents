"""HR manager agent: build the shortlist workbook and mail it to HR."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from datetime import UTC, datetime

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.core.constants import CandidateStatus, EmailLogStatus
from app.utils.excel import ShortlistRow, build_shortlist
from app.utils.mailer import OutboundEmail

logger = structlog.get_logger(__name__)

AGENT = "hr_manager_agent"
_USE_CONTEXT_RUN = object()


def _unique_sheet_titles(jobs) -> dict[str, str]:  # noqa: ANN001
    titles: dict[str, str] = {}
    used: set[str] = set()
    for job in jobs:
        base = (job.title or "Candidates")[:31]
        title = base
        if title in used:
            title = f"{base[:24]} ({job.id[:6]})"
        used.add(title)
        titles[job.id] = title
    return titles


def _marker_path(ctx: AgentContext, idempotency_key: str):
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return (
        ctx.storage.root
        / "reports"
        / (ctx.run_id or "manual")
        / f"shortlist-{digest}.sent"
    )


def build_shortlist_file(
    ctx: AgentContext,
    *,
    limit: int = 500,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    email_run_id: str | None | object = _USE_CONTEXT_RUN,
) -> AgentResult:
    counts = {"candidates": 0, "emailed": 0, "skipped": 0}
    errors: list[dict] = []

    jobs = [ctx.services.jobs.get(job_id)] if job_id else ctx.services.jobs.open_jobs()
    sheet_titles = _unique_sheet_titles(jobs)
    groups: dict[str, list[ShortlistRow]] = {}

    for job in jobs:
        rows: list[ShortlistRow] = []
        for status in (CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED):
            candidates, _total = ctx.services.candidates.list(
                status=status, job_id=job.id, page_size=limit, sort="-score"
            )
            for candidate in candidates:
                if candidate.score is None:
                    continue
                rows.append(
                    ShortlistRow(
                        candidate_name=candidate.full_name,
                        score=candidate.score,
                        matched_skills=candidate.matched_skills,
                        missing_skills=candidate.missing_skills,
                        reason=candidate.reason or "",
                        decision=status,
                    )
                )
        if rows:
            groups[sheet_titles[job.id]] = rows
            counts["candidates"] += len(rows)

    if not groups:
        return AgentResult(counts=counts)

    marker = _marker_path(ctx, idempotency_key) if idempotency_key else None
    if marker is not None and marker.exists():
        counts["skipped"] = 1
        return AgentResult(counts=counts)

    path = ctx.storage.root / "reports" / (ctx.run_id or "manual") / "shortlist.xlsx"
    try:
        build_shortlist(path, groups)
    except Exception as exc:  # noqa: BLE001
        logger.error("shortlist_build_failed", error=str(exc)[:300])
        errors.append({"node": "build_shortlist", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    # Claim before sending. A crash after this point can lose one email, but a
    # retry cannot send a duplicate. The marker is removed only when SMTP fails.
    if marker is not None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("sending\n", encoding="utf-8")
        except OSError as exc:
            logger.error("shortlist_marker_failed", error=str(exc)[:300])
            errors.append({"node": "build_shortlist", "error": str(exc)[:500]})
            return AgentResult(counts=counts, errors=errors)

    if job_id and len(jobs) == 1:
        title = " ".join((jobs[0].title or "Job").split())[:80]
        subject = f"Shortlist - {title} - {datetime.now(UTC).date().isoformat()}"
    else:
        subject = f"Shortlist - {datetime.now(UTC).date().isoformat()}"
    log_run_id = ctx.run_id if email_run_id is _USE_CONTEXT_RUN else email_run_id
    try:
        result = ctx.mailer.send(
            OutboundEmail(
                to=[ctx.settings.hr_admin_email],
                subject=subject,
                body_text=(
                    f"Hello HR,\n\nPlease find the shortlist for {len(groups)} job(s) "
                    f"({counts['candidates']} candidates) attached.\n\n"
                    "Draft emails are waiting for your review and approval.\n\n"
                    "Regards,\nHR Manager Agent"
                ),
                attachments=[str(path)],
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("shortlist_email_failed", error=str(exc)[:300])
        if marker is not None:
            with suppress(OSError):
                marker.unlink()
        ctx.services.emails.record_outbound(
            to_address=ctx.settings.hr_admin_email,
            subject=subject,
            status=EmailLogStatus.FAILED,
            actor=ctx.actor,
            run_id=log_run_id,
            error=str(exc)[:1000],
        )
        ctx.alerts.send(
            "smtp_failed", "Shortlist email failed", f"Could not send the shortlist: {exc}"
        )
        errors.append({"node": "build_shortlist", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    ctx.services.emails.record_outbound(
        to_address=ctx.settings.hr_admin_email,
        subject=subject,
        status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
        actor=ctx.actor,
        run_id=log_run_id,
        message_id=result.message_id,
        from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
    )
    counts["emailed"] = 1
    return AgentResult(counts=counts, errors=errors)
