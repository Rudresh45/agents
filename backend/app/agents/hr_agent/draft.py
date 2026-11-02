"""HR agent: draft interview invitations and rejections.

Drafts are never sent here. They are created unapproved so HR can edit and
approve them before the send step (R6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus, DraftType
from app.schemas.llm import DraftEmail
from app.utils.redaction import redact_pii

logger = structlog.get_logger(__name__)

AGENT = "hr_agent"

PLAN = [
    (CandidateStatus.SHORTLISTED, DraftType.INTERVIEW_INVITE, "draft_invite"),
    (CandidateStatus.NOT_SHORTLISTED, DraftType.REJECTION, "draft_rejection"),
]


def _user_prompt(job, candidate, draft_type: str) -> str:  # noqa: ANN001
    parts = [
        f"Role: {job.title}",
        f"Required skills: {', '.join(job.required_skills) or 'not listed'}",
        f"Location: {job.location or 'not listed'}",
        f"Candidate summary: {candidate.summary or 'not available'}",
        "",
    ]
    if draft_type == DraftType.INTERVIEW_INVITE:
        parts.append(
            "Write the invitation. Use [date], [time] and [location or link] as placeholders."
        )
    else:
        parts.append("Write the rejection. Do not mention scores or internal notes.")
    return "\n".join(parts)


def draft_decisions(ctx: AgentContext, *, limit: int = 200) -> AgentResult:
    counts = {"drafts": 0, "skipped": 0}
    errors: list[dict] = []

    jobs = ctx.services.jobs.open_jobs()
    for job in jobs:
        for status, draft_type, prompt_name in PLAN:
            candidates, _total = ctx.services.candidates.list(
                status=status, job_id=job.id, page_size=limit, sort="created_at"
            )
            for candidate in candidates:
                if ctx.services.drafts.active_for(candidate.id, draft_type):
                    counts["skipped"] += 1
                    continue
                prompt = get_prompt(prompt_name)
                try:
                    draft = ctx.llm.invoke(
                        DraftEmail,
                        system=prompt.full_system,
                        user=_user_prompt(job, candidate, draft_type),
                        prompt_version=prompt.version,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "draft_generation_failed",
                        candidate_id=candidate.id,
                        draft_type=draft_type,
                        error=str(exc)[:200],
                    )
                    errors.append(
                        {
                            "node": "draft_decisions",
                            "item": candidate.id,
                            "error": str(exc)[:500],
                        }
                    )
                    continue

                try:
                    ctx.services.drafts.create_draft(
                        candidate_id=candidate.id,
                        draft_type=draft_type,
                        subject=draft.subject,
                        body=draft.body,
                        actor=ctx.actor,
                        run_id=ctx.run_id,
                        model_name=ctx.settings.gemini_model,
                        prompt_version=prompt.version,
                    )
                    counts["drafts"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "node": "draft_decisions",
                            "item": candidate.id,
                            "error": str(exc)[:500],
                        }
                    )

    return AgentResult(counts=counts, errors=errors)


@dataclass(frozen=True)
class DraftWorkItem:
    candidate_id: str
    job_id: str
    draft_type: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


@dataclass
class DraftPreparation:
    items: list[DraftWorkItem] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"selected": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class DraftInput:
    candidate_id: str
    draft_type: str
    result: DraftEmail
    model_name: str
    prompt_version: str


@dataclass
class DraftApplication:
    counts: dict[str, int] = field(
        default_factory=lambda: {"drafts": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _user_prompt_text(job, summary: str, draft_type: str) -> str:  # noqa: ANN001
    parts = [
        f"Role: {job.title}",
        f"Required skills: {', '.join(job.required_skills) or 'not listed'}",
        f"Location: {job.location or 'not listed'}",
        f"Candidate summary: {summary or 'not available'}",
        "",
    ]
    if draft_type == DraftType.INTERVIEW_INVITE:
        parts.append(
            "Write the invitation. Use [date], [time] and [location or link] as placeholders."
        )
    else:
        parts.append("Write the rejection. Do not mention scores or internal notes.")
    return "\n".join(parts)


def prepare_drafts(
    ctx: AgentContext, *, job_id: str | None = None, limit: int = 200
) -> DraftPreparation:
    """Select decided candidates and return only redacted summary input."""
    preparation = DraftPreparation()
    jobs = [ctx.services.jobs.get(job_id)] if job_id else ctx.services.jobs.open_jobs()
    schema = DraftEmail.model_json_schema()

    for job in jobs:
        if job.status != "open":
            continue
        for status, draft_type, prompt_name in PLAN:
            candidates, _total = ctx.services.candidates.list(
                status=status, job_id=job.id, page_size=limit, sort="created_at"
            )
            prompt = get_prompt(prompt_name)
            for candidate in candidates:
                if ctx.services.drafts.active_for(candidate.id, draft_type) is not None:
                    preparation.counts["skipped"] += 1
                    preparation.skipped.append(
                        {"candidate_id": candidate.id, "reason": "Draft already exists"}
                    )
                    continue
                try:
                    redacted = redact_pii(
                        candidate.summary or "", known_name=candidate.full_name
                    )
                    preparation.items.append(
                        DraftWorkItem(
                            candidate_id=candidate.id,
                            job_id=job.id,
                            draft_type=draft_type,
                            system_prompt=prompt.full_system,
                            user_prompt=_user_prompt_text(job, redacted.text, draft_type),
                            prompt_version=prompt.version,
                            response_schema=schema,
                        )
                    )
                    preparation.counts["selected"] += 1
                except Exception as exc:  # noqa: BLE001
                    preparation.counts["failed"] += 1
                    preparation.errors.append(
                        {
                            "node": "draft_prepare",
                            "item": candidate.id,
                            "error": str(exc)[:500],
                        }
                    )

    return preparation


def apply_drafts(ctx: AgentContext, *, items: list[DraftInput]) -> DraftApplication:
    """Validate and save drafts. Safe to repeat."""
    application = DraftApplication()
    expected_status = {
        DraftType.INTERVIEW_INVITE: CandidateStatus.SHORTLISTED,
        DraftType.REJECTION: CandidateStatus.NOT_SHORTLISTED,
    }

    for item in items:
        if item.draft_type not in expected_status:
            application.counts["failed"] += 1
            application.errors.append(
                {
                    "node": "draft_apply",
                    "item": item.candidate_id,
                    "error": f"Unknown draft type: {item.draft_type}",
                }
            )
            continue

        try:
            candidate = ctx.services.candidates.get(item.candidate_id)
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "draft_apply", "item": item.candidate_id, "error": str(exc)[:500]}
            )
            continue

        if candidate.status != expected_status[item.draft_type]:
            application.counts["skipped"] += 1
            application.skipped.append(
                {
                    "candidate_id": candidate.id,
                    "reason": f"Candidate status is '{candidate.status}'",
                }
            )
            continue
        if ctx.services.drafts.active_for(candidate.id, item.draft_type) is not None:
            application.counts["skipped"] += 1
            application.skipped.append(
                {"candidate_id": candidate.id, "reason": "Draft already exists"}
            )
            continue

        try:
            ctx.services.drafts.create_draft(
                candidate_id=candidate.id,
                draft_type=item.draft_type,
                subject=item.result.subject,
                body=item.result.body,
                actor=ctx.actor,
                run_id=ctx.run_id,
                model_name=item.model_name,
                prompt_version=item.prompt_version,
            )
            application.counts["drafts"] += 1
        except Exception as exc:  # noqa: BLE001
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "draft_apply", "item": candidate.id, "error": str(exc)[:500]}
            )

    return application
