"""HR manager agent: score forwarded candidates against their job."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from app.agents.contracts import AgentContext, AgentResult, Services
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus
from app.core.exceptions import LLMNotConfiguredError
from app.schemas.domain import Actor
from app.schemas.llm import ScoreResult
from app.utils.redaction import redact_pii

logger = structlog.get_logger(__name__)

AGENT = "hr_manager_agent"


def _job_block(job) -> str:  # noqa: ANN001
    return "\n".join(
        [
            f"Title: {job.title}",
            f"Required skills: {', '.join(job.required_skills) or 'not listed'}",
            f"Preferred skills: {', '.join(job.preferred_skills) or 'not listed'}",
            f"Minimum experience: {job.min_experience_years} year(s)",
            f"Education: {job.education or 'not specified'}",
            f"Location: {job.location or 'not specified'}",
            f"Description: {(job.description or '')[:1000]}",
        ]
    )


def _user_prompt(job, resume_text: str) -> str:  # noqa: ANN001
    return (
        f"<job>\n{_job_block(job)}\n</job>\n\n"
        f"<resume>\n{resume_text[:12000]}\n</resume>"
    )


@dataclass(frozen=True)
class ScreeningWorkItem:
    """One candidate's sanitised input for a Kestra AI scoring task."""

    candidate_id: str
    job_id: str
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


@dataclass
class ScreeningPreparation:
    items: list[ScreeningWorkItem] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"selected": 0, "skipped": 0, "failed": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stopped: bool = False


@dataclass(frozen=True)
class ScreeningResultInput:
    """Validated model output for one candidate, ready to apply."""

    candidate_id: str
    result: ScoreResult
    model_name: str
    prompt_version: str


@dataclass
class ScreeningApplication:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "shortlisted": 0,
            "not_shortlisted": 0,
        }
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    job_ids: set[str] = field(default_factory=set)


def prepare_screening(
    services: Services,
    *,
    job_id: str | None = None,
    limit: int = 200,
    actor: Actor,
    run_id: str | None = None,
) -> ScreeningPreparation:
    """Select forwarded candidates and return only sanitised AI input.

    The endpoint is safe to repeat: once a candidate leaves ``forwarded`` it is
    no longer selected. A candidate with no readable resume is moved to
    ``needs_review`` exactly once, matching the old in-process path.
    """

    preparation = ScreeningPreparation()

    if job_id:
        job = services.jobs.get(job_id)
        if job.status != "open":
            preparation.skipped.append({"job_id": job.id, "reason": "Job is closed"})
            preparation.counts["skipped"] += 1
            preparation.stopped = True
            return preparation
        jobs = [job]
    else:
        jobs = services.jobs.open_jobs()
        if not jobs:
            preparation.stopped = True
            return preparation

    prompt = get_prompt("screening")
    response_schema = ScoreResult.model_json_schema()

    for job in jobs:
        candidates, _total = services.candidates.list(
            status=CandidateStatus.FORWARDED,
            job_id=job.id,
            page_size=limit,
            sort="created_at",
        )
        for candidate in candidates:
            if not (candidate.resume_text or "").strip():
                try:
                    services.candidates.transition(
                        candidate.id,
                        CandidateStatus.NEEDS_REVIEW,
                        actor=actor,
                        reason="No resume text to score; HR review required",
                        run_id=run_id,
                    )
                    preparation.counts["skipped"] += 1
                    preparation.skipped.append(
                        {
                            "candidate_id": candidate.id,
                            "reason": "No resume text to score; HR review required",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    services.candidates.session.rollback()
                    preparation.counts["failed"] += 1
                    preparation.errors.append(
                        {
                            "node": "screen_prepare",
                            "item": candidate.id,
                            "error": str(exc)[:500],
                        }
                    )
                continue

            try:
                redacted = redact_pii(candidate.resume_text, known_name=candidate.full_name)
                preparation.items.append(
                    ScreeningWorkItem(
                        candidate_id=candidate.id,
                        job_id=job.id,
                        system_prompt=prompt.full_system,
                        user_prompt=_user_prompt(job, redacted.text),
                        prompt_version=prompt.version,
                        response_schema=response_schema,
                    )
                )
                preparation.counts["selected"] += 1
            except Exception as exc:  # noqa: BLE001
                preparation.counts["failed"] += 1
                preparation.errors.append(
                    {
                        "node": "screen_prepare",
                        "item": candidate.id,
                        "error": str(exc)[:500],
                    }
                )

    return preparation


def apply_screening(
    services: Services,
    *,
    results: list[ScreeningResultInput],
    actor: Actor,
    run_id: str | None = None,
) -> ScreeningApplication:
    """Apply validated model scores through the candidate service.

    Repeating the same request is safe. If a previous attempt saved the score
    but failed before the status transition, the existing identical score is
    reused to finish the transition instead of writing a second score audit.
    """

    application = ScreeningApplication()

    for item in results:
        try:
            candidate = services.candidates.get(item.candidate_id)
        except Exception as exc:  # noqa: BLE001
            services.candidates.session.rollback()
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "screen_apply", "item": item.candidate_id, "error": str(exc)[:500]}
            )
            continue

        if not candidate.job_id:
            application.counts["failed"] += 1
            application.errors.append(
                {
                    "node": "screen_apply",
                    "item": candidate.id,
                    "error": "Candidate has no job (R7)",
                }
            )
            continue

        application.job_ids.add(candidate.job_id)

        try:
            job = services.jobs.get(candidate.job_id)
        except Exception as exc:  # noqa: BLE001
            services.candidates.session.rollback()
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "screen_apply", "item": candidate.id, "error": str(exc)[:500]}
            )
            continue

        target = (
            CandidateStatus.SHORTLISTED
            if item.result.score >= job.min_score
            else CandidateStatus.NOT_SHORTLISTED
        )

        if candidate.status != CandidateStatus.FORWARDED:
            if (
                candidate.status == target
                and candidate.score == item.result.score
                and candidate.scored_model == item.model_name
                and candidate.prompt_version == item.prompt_version
            ):
                application.counts["skipped"] += 1
                application.skipped.append(
                    {"candidate_id": candidate.id, "reason": "Already applied"}
                )
            else:
                application.counts["skipped"] += 1
                application.skipped.append(
                    {
                        "candidate_id": candidate.id,
                        "reason": f"Candidate status is '{candidate.status}'",
                    }
                )
            continue

        if job.status != "open":
            application.counts["skipped"] += 1
            application.skipped.append(
                {"candidate_id": candidate.id, "reason": "Job is closed"}
            )
            continue

        try:
            if candidate.score is not None:
                if (
                    candidate.score != item.result.score
                    or candidate.scored_model != item.model_name
                    or candidate.prompt_version != item.prompt_version
                ):
                    application.counts["skipped"] += 1
                    application.skipped.append(
                        {
                            "candidate_id": candidate.id,
                            "reason": "Candidate already has a different score",
                        }
                    )
                    continue
            else:
                services.candidates.save_score(
                    candidate.id,
                    score=item.result.score,
                    reason=item.result.reason,
                    matched_skills=item.result.matched_skills,
                    missing_skills=item.result.missing_skills,
                    model_name=item.model_name,
                    prompt_version=item.prompt_version,
                    actor=actor,
                    run_id=run_id,
                )

            services.candidates.transition(
                candidate.id,
                target,
                actor=actor,
                reason=f"Screen score {item.result.score} vs threshold {job.min_score}",
                run_id=run_id,
            )
            application.counts["applied"] += 1
            application.counts[
                "shortlisted" if target == CandidateStatus.SHORTLISTED else "not_shortlisted"
            ] += 1
        except Exception as exc:  # noqa: BLE001
            services.candidates.session.rollback()
            application.counts["failed"] += 1
            application.errors.append(
                {"node": "screen_apply", "item": candidate.id, "error": str(exc)[:500]}
            )

    return application


def screen_forwarded(ctx: AgentContext, *, limit: int = 200) -> AgentResult:
    counts = {"scored": 0, "shortlisted": 0, "not_shortlisted": 0, "skipped": 0}
    errors: list[dict] = []
    stop = False

    jobs = ctx.services.jobs.open_jobs()
    if not jobs:
        return AgentResult(counts=counts, stop=True)

    prompt = get_prompt("screening")

    for job in jobs:
        candidates, _total = ctx.services.candidates.list(
            status=CandidateStatus.FORWARDED, job_id=job.id, page_size=limit, sort="created_at"
        )
        for candidate in candidates:
            if not (candidate.resume_text or "").strip():
                try:
                    ctx.services.candidates.transition(
                        candidate.id,
                        CandidateStatus.NEEDS_REVIEW,
                        actor=ctx.actor,
                        reason="No resume text to score; HR review required",
                        run_id=ctx.run_id,
                    )
                    counts["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {"node": "screen_candidates", "item": candidate.id, "error": str(exc)[:500]}
                    )
                continue

            redacted = redact_pii(candidate.resume_text, known_name=candidate.full_name)
            user = _user_prompt(job, redacted.text)
            try:
                result = ctx.llm.invoke(
                    ScoreResult,
                    system=prompt.full_system,
                    user=user,
                    prompt_version=prompt.version,
                )
            except LLMNotConfiguredError as exc:
                ctx.alerts.send(
                    "llm_failed",
                    "LLM is not configured",
                    "Screening stopped because no Gemini API key is configured.",
                )
                errors.append(
                    {"node": "screen_candidates", "error": str(exc)[:500]}
                )
                stop = True
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "screening_failed", candidate_id=candidate.id, error=str(exc)[:200]
                )
                errors.append(
                    {"node": "screen_candidates", "item": candidate.id, "error": str(exc)[:500]}
                )
                continue

            try:
                ctx.services.candidates.save_score(
                    candidate.id,
                    score=result.score,
                    reason=result.reason,
                    matched_skills=result.matched_skills,
                    missing_skills=result.missing_skills,
                    model_name=ctx.settings.gemini_model,
                    prompt_version=prompt.version,
                    actor=ctx.actor,
                    run_id=ctx.run_id,
                )
                target = (
                    CandidateStatus.SHORTLISTED
                    if result.score >= job.min_score
                    else CandidateStatus.NOT_SHORTLISTED
                )
                ctx.services.candidates.transition(
                    candidate.id,
                    target,
                    actor=ctx.actor,
                    reason=f"Screen score {result.score} vs threshold {job.min_score}",
                    run_id=ctx.run_id,
                )
                counts["scored"] += 1
                counts[
                    "shortlisted" if target == CandidateStatus.SHORTLISTED else "not_shortlisted"
                ] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"node": "screen_candidates", "item": candidate.id, "error": str(exc)[:500]}
                )

        if stop:
            break

    return AgentResult(counts=counts, errors=errors, stop=stop)
