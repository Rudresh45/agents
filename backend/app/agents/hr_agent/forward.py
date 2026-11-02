"""HR agent: send the daily candidate digest to the HR manager.

The digest is internal mail. Each candidate moves to `forwarded` only after the
mail has been handed to SMTP (or to the dry-run sink).
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus, EmailLogStatus
from app.schemas.llm import CandidateSummary
from app.utils.idempotency import claim_marker, marker_path, release_marker
from app.utils.mailer import OutboundEmail
from app.utils.redaction import redact_pii

logger = structlog.get_logger(__name__)

AGENT = "hr_agent"


def _summary_for(ctx: AgentContext, candidate) -> tuple[str, str | None]:  # noqa: ANN001
    text = candidate.resume_text or ""
    if not text.strip():
        return "No resume text available.", "Resume text is empty"
    redacted = redact_pii(text, known_name=candidate.full_name)
    prompt = get_prompt("summarise")
    try:
        result = ctx.llm.invoke(
            CandidateSummary,
            system=prompt.full_system,
            user=f"<resume>\n{redacted.text[:6000]}\n</resume>",
            prompt_version=prompt.version,
        )
    except Exception as exc:  # noqa: BLE001
        return "Summary unavailable.", str(exc)[:300]
    return result.summary, None


def _digest_body(rows: list[dict], run_id: str | None) -> str:
    lines = [
        "Hello HR Manager,",
        "",
        f"{len(rows)} new application(s) from run {run_id or 'manual'} are ready for screening.",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['name']} - {row['role']}")
        if row.get("score") is not None:
            lines.append(f"   Score: {row['score']}")
        lines.append(f"   Summary: {row['summary']}")
        lines.append("")
    lines.extend(
        [
            "Resumes are attached.",
            "",
            "Regards,",
            "HR Agent",
        ]
    )
    return "\n".join(lines)


def forward_candidates(ctx: AgentContext, *, limit: int = 200) -> AgentResult:
    counts = {"forwarded": 0, "skipped": 0, "digest_sent": 0}
    errors: list[dict] = []

    candidates, _total = ctx.services.candidates.list(
        status=CandidateStatus.RECEIVED, page_size=limit, sort="created_at"
    )
    if not candidates:
        return AgentResult(counts=counts)

    rows: list[dict] = []
    attachments: list[str] = []
    forwardable = []

    for candidate in candidates:
        if candidate.job_id is None:
            counts["skipped"] += 1
            continue
        if not candidate.resume_text:
            try:
                ctx.services.candidates.transition(
                    candidate.id,
                    CandidateStatus.NEEDS_RESUBMISSION,
                    actor=ctx.actor,
                    reason="Resume text is missing",
                    run_id=ctx.run_id,
                )
                counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"node": "forward_to_manager", "item": candidate.id, "error": str(exc)[:500]}
                )
            continue

        summary, error = _summary_for(ctx, candidate)
        if error:
            errors.append(
                {"node": "forward_to_manager", "item": candidate.id, "error": error}
            )
        try:
            ctx.services.candidates.set_summary(candidate.id, summary)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"node": "forward_to_manager", "item": candidate.id, "error": str(exc)[:500]}
            )

        try:
            job = ctx.services.jobs.get(candidate.job_id)
            role = job.title
        except Exception:  # noqa: BLE001
            role = "Unknown role"

        rows.append(
            {
                "name": candidate.full_name,
                "role": role,
                "score": candidate.score,
                "summary": summary,
            }
        )
        forwardable.append(candidate)

        if candidate.resume_path:
            with suppress(Exception):
                attachments.append(str(ctx.storage.resolve(candidate.resume_path)))

    if not forwardable:
        return AgentResult(counts=counts, errors=errors)

    subject = f"Candidate digest - {datetime.now(UTC).date().isoformat()}"
    body = _digest_body(rows, ctx.run_id)
    try:
        result = ctx.mailer.send(
            OutboundEmail(
                to=[ctx.settings.hr_manager_notify_email],
                subject=subject,
                body_text=body,
                attachments=attachments,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("digest_send_failed", error=str(exc)[:300])
        ctx.services.emails.record_outbound(
            to_address=ctx.settings.hr_manager_notify_email,
            subject=subject,
            status=EmailLogStatus.FAILED,
            actor=ctx.actor,
            run_id=ctx.run_id,
            error=str(exc)[:1000],
        )
        ctx.alerts.send(
            "smtp_failed",
            "Candidate digest could not be sent",
            f"The digest with {len(forwardable)} candidate(s) failed: {exc}",
        )
        errors.append({"node": "forward_to_manager", "error": str(exc)[:500]})
        return AgentResult(counts=counts, errors=errors)

    ctx.services.emails.record_outbound(
        to_address=ctx.settings.hr_manager_notify_email,
        subject=subject,
        status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
        actor=ctx.actor,
        run_id=ctx.run_id,
        message_id=result.message_id,
        from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
    )
    counts["digest_sent"] = 1

    for candidate in forwardable:
        try:
            ctx.services.candidates.transition(
                candidate.id,
                CandidateStatus.FORWARDED,
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            counts["forwarded"] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"node": "forward_to_manager", "item": candidate.id, "error": str(exc)[:500]}
            )

    return AgentResult(counts=counts, errors=errors)


@dataclass(frozen=True)
class ForwardWorkItem:
    """One candidate's sanitised resume input for a Kestra summary task."""

    candidate_id: str
    job_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


@dataclass
class ForwardPreparation:
    items: list[ForwardWorkItem] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"selected": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class ForwardSummaryInput:
    candidate_id: str
    result: CandidateSummary
    model_name: str
    prompt_version: str


@dataclass
class ForwardApplication:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "applied": 0,
            "skipped": 0,
            "forwarded": 0,
            "digest_sent": 0,
            "failed": 0,
        }
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def prepare_forward(ctx: AgentContext, *, limit: int = 200) -> ForwardPreparation:
    """Select received candidates and return only redacted resume text."""
    preparation = ForwardPreparation()
    candidates, _total = ctx.services.candidates.list(
        status=CandidateStatus.RECEIVED, page_size=limit, sort="created_at"
    )
    prompt = get_prompt("summarise")
    schema = CandidateSummary.model_json_schema()

    for candidate in candidates:
        if candidate.job_id is None:
            preparation.counts["skipped"] += 1
            preparation.skipped.append(
                {"candidate_id": candidate.id, "reason": "No job assigned"}
            )
            continue
        if not (candidate.resume_text or "").strip():
            try:
                ctx.services.candidates.transition(
                    candidate.id,
                    CandidateStatus.NEEDS_RESUBMISSION,
                    actor=ctx.actor,
                    reason="Resume text is missing",
                    run_id=ctx.run_id,
                )
                preparation.counts["skipped"] += 1
                preparation.skipped.append(
                    {"candidate_id": candidate.id, "reason": "Resume text is missing"}
                )
            except Exception as exc:  # noqa: BLE001
                preparation.counts["failed"] += 1
                preparation.errors.append(
                    {
                        "node": "forward_prepare",
                        "item": candidate.id,
                        "error": str(exc)[:500],
                    }
                )
            continue

        try:
            redacted = redact_pii(candidate.resume_text, known_name=candidate.full_name)
            preparation.items.append(
                ForwardWorkItem(
                    candidate_id=candidate.id,
                    job_id=candidate.job_id,
                    system_prompt=prompt.full_system,
                    user_prompt=f"<resume>\n{redacted.text[:6000]}\n</resume>",
                    prompt_version=prompt.version,
                    response_schema=schema,
                )
            )
            preparation.candidate_ids.append(candidate.id)
            preparation.counts["selected"] += 1
        except Exception as exc:  # noqa: BLE001
            preparation.counts["failed"] += 1
            preparation.errors.append(
                {
                    "node": "forward_prepare",
                    "item": candidate.id,
                    "error": str(exc)[:500],
                }
            )

    return preparation


def _eligible_for_digest(ctx: AgentContext, candidate_ids: list[str]) -> list:  # noqa: ANN202
    candidates = []
    if candidate_ids:
        for candidate_id in candidate_ids:
            with suppress(Exception):
                candidates.append(ctx.services.candidates.get(candidate_id))
    else:
        candidates, _total = ctx.services.candidates.list(
            status=CandidateStatus.RECEIVED, page_size=500, sort="created_at"
        )
    return [
        candidate
        for candidate in candidates
        if candidate.status == CandidateStatus.RECEIVED
        and candidate.job_id is not None
        and (candidate.resume_text or "").strip()
        and (candidate.summary or "").strip()
    ]


def _transition_forwarded(
    ctx: AgentContext, candidates: list, counts: dict, errors: list[dict]
) -> None:
    for candidate in candidates:
        try:
            ctx.services.candidates.transition(
                candidate.id,
                CandidateStatus.FORWARDED,
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            counts["forwarded"] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"node": "forward_apply", "item": candidate.id, "error": str(exc)[:500]}
            )


def apply_forward(
    ctx: AgentContext,
    *,
    summaries: list[ForwardSummaryInput],
    candidate_ids: list[str] | None = None,
    finalize: bool = True,
    idempotency_key: str | None = None,
) -> ForwardApplication:
    """Save summaries, then send one digest and move candidates to forwarded.

    A retry cannot send a second digest: a marker is claimed before SMTP. If a
    previous attempt sent the digest but failed while updating statuses, the
    retry finishes the transitions without mailing again.
    """
    application = ForwardApplication()

    for item in summaries:
        try:
            candidate = ctx.services.candidates.get(item.candidate_id)
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "forward_apply", "item": item.candidate_id, "error": str(exc)[:500]}
            )
            continue

        if candidate.status != CandidateStatus.RECEIVED:
            application.counts["skipped"] += 1
            application.skipped.append(
                {
                    "candidate_id": candidate.id,
                    "reason": f"Candidate status is '{candidate.status}'",
                }
            )
            continue

        if candidate.summary == item.result.summary:
            application.counts["skipped"] += 1
            application.skipped.append(
                {"candidate_id": candidate.id, "reason": "Summary already applied"}
            )
            continue
        if candidate.summary:
            application.counts["skipped"] += 1
            application.skipped.append(
                {"candidate_id": candidate.id, "reason": "Candidate already has a summary"}
            )
            continue

        try:
            ctx.services.candidates.set_summary(
                candidate.id,
                item.result.summary,
                actor=ctx.actor,
                run_id=ctx.run_id,
                model_name=item.model_name,
                prompt_version=item.prompt_version,
            )
            application.counts["applied"] += 1
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "forward_apply", "item": candidate.id, "error": str(exc)[:500]}
            )

    if not finalize:
        return application

    eligible = _eligible_for_digest(ctx, candidate_ids or [])
    marker = marker_path(
        ctx.storage.root, f"forward-digest:{idempotency_key or ctx.run_id or 'manual'}"
    )
    if marker.exists():
        _transition_forwarded(ctx, eligible, application.counts, application.errors)
        application.counts["skipped"] += 1
        application.skipped.append({"reason": "Digest already sent"})
        return application

    if not eligible:
        return application

    rows: list[dict] = []
    attachments: list[str] = []
    for candidate in eligible:
        try:
            role = ctx.services.jobs.get(candidate.job_id).title
        except Exception:  # noqa: BLE001
            role = "Unknown role"
        rows.append(
            {
                "name": candidate.full_name,
                "role": role,
                "score": candidate.score,
                "summary": candidate.summary,
            }
        )
        if candidate.resume_path:
            with suppress(Exception):
                attachments.append(str(ctx.storage.resolve(candidate.resume_path)))

    if not claim_marker(marker):
        application.counts["skipped"] += 1
        application.skipped.append({"reason": "Digest is already being sent"})
        return application

    subject = f"Candidate digest - {datetime.now(UTC).date().isoformat()}"
    body = _digest_body(rows, ctx.run_id)
    try:
        result = ctx.mailer.send(
            OutboundEmail(
                to=[ctx.settings.hr_manager_notify_email],
                subject=subject,
                body_text=body,
                attachments=attachments,
            )
        )
    except Exception as exc:  # noqa: BLE001
        release_marker(marker)
        logger.error("digest_send_failed", error=str(exc)[:300])
        ctx.services.emails.record_outbound(
            to_address=ctx.settings.hr_manager_notify_email,
            subject=subject,
            status=EmailLogStatus.FAILED,
            actor=ctx.actor,
            run_id=ctx.run_id,
            error=str(exc)[:1000],
        )
        ctx.alerts.send(
            "smtp_failed",
            "Candidate digest could not be sent",
            f"The digest with {len(eligible)} candidate(s) failed: {exc}",
        )
        application.counts["failed"] += 1
        application.errors.append(
            {"node": "forward_apply", "error": str(exc)[:500]}
        )
        return application

    ctx.services.emails.record_outbound(
        to_address=ctx.settings.hr_manager_notify_email,
        subject=subject,
        status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
        actor=ctx.actor,
        run_id=ctx.run_id,
        message_id=result.message_id,
        from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
    )
    application.counts["digest_sent"] = 1
    _transition_forwarded(ctx, eligible, application.counts, application.errors)
    return application
