"""CEO report agent: approved shortlist only, no contact details.

Runs after the send step. It refuses to run while an approved draft is still
unsent, which keeps the CEO report downstream of the human approval gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus, EmailLogStatus
from app.schemas.domain import ReportItemInput
from app.schemas.llm import CeoSummary
from app.utils.excel import CeoReportRow, build_ceo_report
from app.utils.idempotency import claim_marker, marker_path, release_marker
from app.utils.mailer import OutboundEmail
from app.utils.redaction import redact_pii

logger = structlog.get_logger(__name__)

AGENT = "ceo_report_agent"


def generate_report(
    ctx: AgentContext, *, job_ids: list[str] | None = None, send: bool = True
) -> AgentResult:
    counts = {"candidates": 0, "emailed": 0}
    errors: list[dict] = []

    pending = ctx.services.drafts.list_approved(run_id=ctx.run_id)
    if pending:
        errors.append(
            {
                "node": "ceo_report",
                "error": f"{len(pending)} approved draft(s) are still unsent; report blocked",
            }
        )
        return AgentResult(counts=counts, errors=errors, stop=True)

    jobs, _total = ctx.services.jobs.list(page_size=500)
    if job_ids:
        wanted = set(job_ids)
        jobs = [job for job in jobs if job.id in wanted]
    if not jobs:
        return AgentResult(counts=counts)

    accepted, _total = ctx.services.candidates.list(
        status=CandidateStatus.ACCEPTED, page_size=500, sort="-score"
    )
    if job_ids:
        wanted = set(job_ids)
        accepted = [candidate for candidate in accepted if candidate.job_id in wanted]
    job_titles = {job.id: job.title for job in jobs}

    summary_rows: list[dict] = []
    for job in jobs:
        status_counts = ctx.services.candidates.counts_by_status(job.id)
        applications = sum(status_counts.values())
        if applications == 0:
            continue
        summary_rows.append(
            {
                "job": job.title,
                "applications": applications,
                "shortlisted": status_counts.get("shortlisted", 0)
                + status_counts.get("accepted", 0),
                "accepted": status_counts.get("accepted", 0),
                "rejected": status_counts.get("rejected", 0),
            }
        )

    if not summary_rows:
        return AgentResult(counts=counts)

    rows: list[CeoReportRow] = []
    items: list[ReportItemInput] = []
    for candidate in accepted:
        title = job_titles.get(candidate.job_id, "Unassigned")
        summary = candidate.summary or candidate.reason or ""
        rows.append(
            CeoReportRow(
                candidate_name=candidate.full_name,
                job_title=title,
                score=candidate.score,
                matched_skills=candidate.matched_skills,
                missing_skills=candidate.missing_skills,
                summary=summary[:600],
                decision=candidate.status,
            )
        )
        items.append(
            ReportItemInput(
                candidate_id=candidate.id,
                job_title=title,
                candidate_name=candidate.full_name,
                score=candidate.score,
                matched_skills=candidate.matched_skills,
                missing_skills=candidate.missing_skills,
                summary=summary[:600],
                decision=candidate.status,
            )
        )

    title = f"Approved shortlist - {datetime.now(UTC).date().isoformat()}"
    folder = ctx.run_id or "manual"
    path = ctx.storage.root / "reports" / folder / "ceo_report.xlsx"
    try:
        build_ceo_report(path, title=title, summary_rows=summary_rows, rows=rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("ceo_report_build_failed", error=str(exc)[:300])
        errors.append({"node": "ceo_report", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    relative = path.relative_to(ctx.storage.root).as_posix()
    try:
        report = ctx.services.reports.save_report(
            run_id=ctx.run_id,
            job_id=None,
            file_path=relative,
            format="xlsx",
            sent_to=ctx.settings.ceo_report_email,
            items=items,
            actor=ctx.actor,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append({"node": "ceo_report", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    if not send:
        counts["candidates"] = len(rows)
        return AgentResult(counts=counts, data={"report_id": report.id})

    try:
        result = ctx.mailer.send(
            OutboundEmail(
                to=[ctx.settings.ceo_report_email],
                subject=title,
                body_text=(
                    "Hello,\n\nPlease find the approved shortlist attached. It contains "
                    "only candidates whose decision email was sent after HR approval. "
                    "Contact details and protected characteristics are excluded.\n\n"
                    "Regards,\nHR Team"
                ),
                attachments=[str(path)],
            )
        )
    except Exception as exc:  # noqa: BLE001
        ctx.services.reports.mark_failed(report.id, error=str(exc), actor=ctx.actor)
        ctx.services.emails.record_outbound(
            to_address=ctx.settings.ceo_report_email,
            subject=title,
            status=EmailLogStatus.FAILED,
            actor=ctx.actor,
            run_id=ctx.run_id,
            error=str(exc)[:1000],
        )
        ctx.alerts.send(
            "smtp_failed", "CEO report email failed", f"Could not send the report: {exc}"
        )
        errors.append({"node": "ceo_report", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    ctx.services.emails.record_outbound(
        to_address=ctx.settings.ceo_report_email,
        subject=title,
        status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
        actor=ctx.actor,
        run_id=ctx.run_id,
        message_id=result.message_id,
        from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
    )
    ctx.services.reports.mark_sent(
        report.id, sent_to=ctx.settings.ceo_report_email, actor=ctx.actor
    )
    counts["candidates"] = len(rows)
    counts["emailed"] = 1
    return AgentResult(counts=counts, errors=errors, data={"report_id": report.id})


@dataclass(frozen=True)
class ReportWorkItem:
    candidate_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


@dataclass
class ReportPreparation:
    items: list[ReportWorkItem] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"selected": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stopped: bool = False

    @property
    def has_work(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class ReportSummaryInput:
    candidate_id: str
    result: CeoSummary
    model_name: str
    prompt_version: str


@dataclass
class ReportApplication:
    counts: dict[str, int] = field(
        default_factory=lambda: {"candidates": 0, "emailed": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    report_id: str | None = None


def _collect(ctx: AgentContext, job_ids: list[str] | None):  # noqa: ANN202
    jobs, _total = ctx.services.jobs.list(page_size=500)
    if job_ids:
        wanted = set(job_ids)
        jobs = [job for job in jobs if job.id in wanted]
    if not jobs:
        return [], [], {}, []

    accepted, _total = ctx.services.candidates.list(
        status=CandidateStatus.ACCEPTED, page_size=500, sort="-score"
    )
    if job_ids:
        wanted = set(job_ids)
        accepted = [candidate for candidate in accepted if candidate.job_id in wanted]
    job_titles = {job.id: job.title for job in jobs}

    summary_rows: list[dict] = []
    for job in jobs:
        status_counts = ctx.services.candidates.counts_by_status(job.id)
        applications = sum(status_counts.values())
        if applications == 0:
            continue
        summary_rows.append(
            {
                "job": job.title,
                "applications": applications,
                "shortlisted": status_counts.get("shortlisted", 0)
                + status_counts.get("accepted", 0),
                "accepted": status_counts.get("accepted", 0),
                "rejected": status_counts.get("rejected", 0),
            }
        )
    return jobs, accepted, job_titles, summary_rows


def prepare_report(
    ctx: AgentContext, *, job_ids: list[str] | None = None
) -> ReportPreparation:
    """Select accepted candidates and return redacted summary input."""
    preparation = ReportPreparation(job_ids=job_ids or [])
    pending = ctx.services.drafts.list_approved(run_id=ctx.run_id)
    if pending:
        preparation.stopped = True
        preparation.errors.append(
            {
                "node": "report_prepare",
                "error": f"{len(pending)} approved draft(s) are still unsent; report blocked",
            }
        )
        return preparation

    _jobs, accepted, _job_titles, summary_rows = _collect(ctx, job_ids)
    if not summary_rows:
        return preparation

    prompt = get_prompt("ceo_summary")
    schema = CeoSummary.model_json_schema()
    for candidate in accepted:
        text = candidate.summary or candidate.reason or candidate.resume_text or ""
        if not text.strip():
            preparation.counts["skipped"] += 1
            preparation.skipped.append(
                {"candidate_id": candidate.id, "reason": "No summary text available"}
            )
            continue
        try:
            redacted = redact_pii(text, known_name=candidate.full_name)
            preparation.items.append(
                ReportWorkItem(
                    candidate_id=candidate.id,
                    system_prompt=prompt.full_system,
                    user_prompt=f"<candidate>\n{redacted.text[:4000]}\n</candidate>",
                    prompt_version=prompt.version,
                    response_schema=schema,
                )
            )
            preparation.counts["selected"] += 1
        except Exception as exc:  # noqa: BLE001
            preparation.counts["failed"] += 1
            preparation.errors.append(
                {
                    "node": "report_prepare",
                    "item": candidate.id,
                    "error": str(exc)[:500],
                }
            )
    return preparation


def apply_report(
    ctx: AgentContext,
    *,
    summaries: list[ReportSummaryInput],
    job_ids: list[str] | None = None,
    send: bool = True,
    idempotency_key: str | None = None,
) -> ReportApplication:
    """Build and send one read-only CEO report for this run. Safe to repeat."""
    application = ReportApplication()
    pending = ctx.services.drafts.list_approved(run_id=ctx.run_id)
    if pending:
        application.counts["failed"] += 1
        application.errors.append(
            {
                "node": "report_apply",
                "error": f"{len(pending)} approved draft(s) are still unsent; report blocked",
            }
        )
        return application

    _jobs, accepted, job_titles, summary_rows = _collect(ctx, job_ids)
    if not summary_rows:
        return application

    wanted = set(job_ids) if job_ids else None
    summary_map: dict[str, str] = {}
    for item in summaries:
        try:
            candidate = ctx.services.candidates.get(item.candidate_id)
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "report_apply", "item": item.candidate_id, "error": str(exc)[:500]}
            )
            continue
        if candidate.status != CandidateStatus.ACCEPTED:
            application.counts["skipped"] += 1
            application.skipped.append(
                {
                    "candidate_id": candidate.id,
                    "reason": f"Candidate status is '{candidate.status}'",
                }
            )
            continue
        if wanted is not None and candidate.job_id not in wanted:
            application.counts["skipped"] += 1
            application.skipped.append(
                {"candidate_id": candidate.id, "reason": "Candidate is outside this job"}
            )
            continue
        try:
            ctx.services.reports.record_summary(
                candidate.id,
                summary=item.result.summary,
                model_name=item.model_name,
                prompt_version=item.prompt_version,
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            summary_map[candidate.id] = item.result.summary
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "report_apply", "item": candidate.id, "error": str(exc)[:500]}
            )

    rows: list[CeoReportRow] = []
    items: list[ReportItemInput] = []
    for candidate in accepted:
        title = job_titles.get(candidate.job_id, "Unassigned")
        summary = (
            summary_map.get(candidate.id)
            or candidate.summary
            or candidate.reason
            or ""
        )
        rows.append(
            CeoReportRow(
                candidate_name=candidate.full_name,
                job_title=title,
                score=candidate.score,
                matched_skills=candidate.matched_skills,
                missing_skills=candidate.missing_skills,
                summary=summary[:600],
                decision=candidate.status,
            )
        )
        items.append(
            ReportItemInput(
                candidate_id=candidate.id,
                job_title=title,
                candidate_name=candidate.full_name,
                score=candidate.score,
                matched_skills=candidate.matched_skills,
                missing_skills=candidate.missing_skills,
                summary=summary[:600],
                decision=candidate.status,
            )
        )

    existing = ctx.services.reports.get_by_run(ctx.run_id) if ctx.run_id else None
    if existing is not None and existing.status == "sent":
        application.counts["skipped"] += 1
        application.skipped.append({"reason": "Report already sent for this run"})
        application.report_id = existing.id
        return application

    title = f"Approved shortlist - {datetime.now(UTC).date().isoformat()}"
    folder = ctx.run_id or "manual"
    path = ctx.storage.root / "reports" / folder / "ceo_report.xlsx"
    try:
        build_ceo_report(path, title=title, summary_rows=summary_rows, rows=rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("ceo_report_build_failed", error=str(exc)[:300])
        application.counts["failed"] += 1
        application.errors.append({"node": "report_apply", "error": str(exc)[:500]})
        return application

    relative = path.relative_to(ctx.storage.root).as_posix()
    try:
        if existing is not None:
            report_id = existing.id
        else:
            report = ctx.services.reports.save_report(
                run_id=ctx.run_id,
                job_id=job_ids[0] if job_ids and len(job_ids) == 1 else None,
                file_path=relative,
                format="xlsx",
                sent_to=ctx.settings.ceo_report_email,
                items=items,
                actor=ctx.actor,
            )
            report_id = report.id
    except Exception as exc:  # noqa: BLE001
        application.counts["failed"] += 1
        application.errors.append({"node": "report_apply", "error": str(exc)[:500]})
        return application

    application.report_id = report_id
    application.counts["candidates"] = len(rows)
    if not send:
        return application

    marker = marker_path(
        ctx.storage.root, f"ceo-report:{idempotency_key or ctx.run_id or 'manual'}"
    )
    if marker.exists():
        application.counts["skipped"] += 1
        application.skipped.append({"reason": "Report already sent"})
        return application
    if not claim_marker(marker):
        application.counts["skipped"] += 1
        application.skipped.append({"reason": "Report is already being sent"})
        return application

    try:
        result = ctx.mailer.send(
            OutboundEmail(
                to=[ctx.settings.ceo_report_email],
                subject=title,
                body_text=(
                    "Hello,\n\nPlease find the approved shortlist attached. It contains "
                    "only candidates whose decision email was sent after HR approval. "
                    "Contact details and protected characteristics are excluded.\n\n"
                    "Regards,\nHR Team"
                ),
                attachments=[str(path)],
            )
        )
    except Exception as exc:  # noqa: BLE001
        release_marker(marker)
        ctx.services.reports.mark_failed(report_id, error=str(exc), actor=ctx.actor)
        ctx.services.emails.record_outbound(
            to_address=ctx.settings.ceo_report_email,
            subject=title,
            status=EmailLogStatus.FAILED,
            actor=ctx.actor,
            run_id=ctx.run_id,
            error=str(exc)[:1000],
        )
        ctx.alerts.send(
            "smtp_failed", "CEO report email failed", f"Could not send the report: {exc}"
        )
        application.counts["failed"] += 1
        application.errors.append({"node": "report_apply", "error": str(exc)[:500]})
        return application

    ctx.services.emails.record_outbound(
        to_address=ctx.settings.ceo_report_email,
        subject=title,
        status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
        actor=ctx.actor,
        run_id=ctx.run_id,
        message_id=result.message_id,
        from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
    )
    ctx.services.reports.mark_sent(
        report_id, sent_to=ctx.settings.ceo_report_email, actor=ctx.actor
    )
    application.counts["emailed"] = 1
    return application
