"""HR agent intake split into prepare / apply for Kestra.

The old ``read_inbox`` path stays untouched behind ``USE_KESTRA_LLM=false``.
This module performs the same work in two idempotent halves:

* prepare: fetch unread mail, redact it, and return only sanitised LLM input.
* apply: validate the model output and save candidates, replies and audit rows.

Raw sender details and attachment metadata never leave the backend. They live
in a short-lived staging file keyed by an opaque hash.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.agents.contracts import AgentContext
from app.agents.hr_agent.inbox import (
    _address,
    _display_name,
    _draft_templates,
    _email_prompt,
    _guess_name,
    _is_reply,
    _phone,
    _role_from_subject,
)
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus, DraftType, EmailLogStatus, ReplyCategory
from app.core.exceptions import DuplicateCandidateError
from app.schemas.domain import ApplicationInput
from app.schemas.llm import EmailClassification, JobMatch, ReplyClassification
from app.utils.imap_client import AttachmentMeta, InboundEmail
from app.utils.redaction import redact_pii
from app.utils.resume_parser import parse_resume
from app.utils.staging import (
    cleanup_stage,
    digest_key,
    read_stage,
    write_stage,
)

logger = structlog.get_logger(__name__)

AGENT = "hr_agent"
STAGE_NAMESPACE = "inbox"


@dataclass(frozen=True)
class PromptBlock:
    system_prompt: str
    user_prompt: str
    prompt_version: str
    response_schema: dict[str, Any]


@dataclass(frozen=True)
class InboxApplicationItem:
    email_key: str
    job_id: str | None
    classification: PromptBlock
    job_match: PromptBlock | None


@dataclass(frozen=True)
class InboxReplyItem:
    email_key: str
    reply: PromptBlock


@dataclass
class InboxPreparation:
    applications: list[InboxApplicationItem] = field(default_factory=list)
    replies: list[InboxReplyItem] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"fetched": 0, "selected": 0, "skipped": 0, "duplicates": 0}
    )
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.applications or self.replies)


@dataclass(frozen=True)
class InboxApplicationInput:
    email_key: str
    classification: EmailClassification
    classification_model: str
    classification_prompt_version: str
    job_match: JobMatch | None = None
    job_match_model: str | None = None
    job_match_prompt_version: str | None = None


@dataclass(frozen=True)
class InboxReplyInput:
    email_key: str
    reply: ReplyClassification
    model_name: str
    prompt_version: str


@dataclass
class InboxApplyOutcome:
    counts: dict[str, int] = field(default_factory=dict)
    candidate_id: str | None = None
    draft_type: str | None = None
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


@dataclass
class AcknowledgeOutcome:
    created: bool = False
    draft_id: str | None = None
    skipped: bool = False
    errors: list[dict] = field(default_factory=list)


def _application_block(prompt, *, user: str, schema: dict[str, Any]) -> PromptBlock:  # noqa: ANN001
    return PromptBlock(
        system_prompt=prompt.full_system,
        user_prompt=user,
        prompt_version=prompt.version,
        response_schema=schema,
    )


def _job_match_prompt(email: InboundEmail, body: str, jobs: list) -> PromptBlock | None:  # noqa: ANN001
    if not jobs:
        return None
    payload = [
        {
            "id": job.id,
            "title": job.title,
            "alias": job.email_alias,
            "required_skills": job.required_skills,
        }
        for job in jobs
    ]
    prompt = get_prompt("match_job")
    user = (
        f"Open jobs:\n{json.dumps(payload)}\n\n"
        f"Stated role: {_role_from_subject(email.subject) or 'not stated'}\n"
        f"Subject: {email.subject}\n"
        f"<email>\n{body[:2000]}\n</email>"
    )
    return _application_block(prompt, user=user, schema=JobMatch.model_json_schema())


def _stage_item(stage: dict[str, Any]) -> InboxApplicationItem | InboxReplyItem:
    if stage["kind"] == "reply":
        return InboxReplyItem(
            email_key=stage["email_key"],
            reply=PromptBlock(**stage["reply"]),
        )
    job_match = stage.get("job_match")
    return InboxApplicationItem(
        email_key=stage["email_key"],
        job_id=stage.get("job_id"),
        classification=PromptBlock(**stage["classification"]),
        job_match=PromptBlock(**job_match) if job_match else None,
    )


def prepare_inbox(ctx: AgentContext, *, limit: int = 50) -> InboxPreparation:
    """Fetch unread mail and return only redacted AI input.

    The method is safe to repeat. A staging record that is still waiting for
    apply is returned again; one that has already been applied is skipped and
    the IMAP message is marked as processed.
    """
    preparation = InboxPreparation()
    cleanup_stage(ctx.settings.upload_dir, STAGE_NAMESPACE)

    try:
        emails = ctx.imap.fetch_unread(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("inbox_fetch_failed", error=str(exc)[:300])
        preparation.errors.append(
            {"node": "inbox_prepare", "error": str(exc)[:500]}
        )
        return preparation

    preparation.counts["fetched"] = len(emails)

    for email in emails:
        key = email.message_id or f"uid:{email.uid}"
        email_key = digest_key(key)
        try:
            if ctx.services.emails.exists_message_id(key, direction="inbound"):
                preparation.counts["duplicates"] += 1
                ctx.imap.mark_processed(email.uid)
                continue

            stage = read_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, email_key)
            if stage is not None:
                if stage.get("applied"):
                    preparation.counts["duplicates"] += 1
                    ctx.imap.mark_processed(email.uid)
                    continue
                item = _stage_item(stage)
                if isinstance(item, InboxReplyItem):
                    preparation.replies.append(item)
                else:
                    preparation.applications.append(item)
                preparation.counts["selected"] += 1
                continue

            if _is_reply(ctx, email):
                candidate = ctx.services.candidates.find_by_email(_address(email.from_address))
                if candidate is None:
                    ctx.services.emails.record_inbound(
                        message_id=email.message_id or f"uid:{email.uid}",
                        from_address=_address(email.from_address),
                        to_address=_address(email.to_address),
                        subject=email.subject,
                        status=EmailLogStatus.SKIPPED,
                        run_id=ctx.run_id,
                    )
                    ctx.imap.mark_processed(email.uid)
                    preparation.counts["skipped"] += 1
                    preparation.skipped.append(
                        {"email_key": email_key, "reason": "Reply from an unknown sender"}
                    )
                    continue

                redacted = redact_pii(email.body_text, known_name=candidate.full_name)
                prompt = get_prompt("classify_reply")
                block = _application_block(
                    prompt,
                    user=_email_prompt(email, redacted.text),
                    schema=ReplyClassification.model_json_schema(),
                )
                stage = {
                    "email_key": email_key,
                    "kind": "reply",
                    "uid": email.uid,
                    "message_id": email.message_id or f"uid:{email.uid}",
                    "from_address": email.from_address,
                    "to_address": email.to_address,
                    "subject": email.subject,
                    "candidate_id": candidate.id,
                    "body_excerpt": redacted.text[:1000],
                    "reply": asdict(block),
                }
                write_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, email_key, stage)
                preparation.replies.append(
                    InboxReplyItem(email_key=email_key, reply=block)
                )
                preparation.counts["selected"] += 1
                continue

            display_name = _display_name(email.from_address)
            redacted = redact_pii(email.body_text, known_name=display_name or None)
            prompt = get_prompt("classify_email")
            classification = _application_block(
                prompt,
                user=_email_prompt(email, redacted.text),
                schema=EmailClassification.model_json_schema(),
            )

            jobs = ctx.services.jobs.open_jobs()
            haystack = f"{email.to_address} {email.subject}".lower()
            job_id = None
            for job in jobs:
                if job.email_alias and job.email_alias.lower() in haystack:
                    job_id = job.id
                    break
            job_match = None if job_id else _job_match_prompt(email, redacted.text, jobs)

            stage = {
                "email_key": email_key,
                "kind": "application",
                "uid": email.uid,
                "message_id": email.message_id or f"uid:{email.uid}",
                "from_address": email.from_address,
                "to_address": email.to_address,
                "subject": email.subject,
                "display_name": display_name,
                "phone": _phone(email.body_text),
                "attachments": [meta.model_dump() for meta in email.attachments],
                "job_id": job_id,
                "open_job_ids": [job.id for job in jobs],
                "classification": asdict(classification),
                "job_match": asdict(job_match) if job_match else None,
            }
            write_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, email_key, stage)
            preparation.applications.append(
                InboxApplicationItem(
                    email_key=email_key,
                    job_id=job_id,
                    classification=classification,
                    job_match=job_match,
                )
            )
            preparation.counts["selected"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("inbox_prepare_item_failed", uid=email.uid)
            preparation.errors.append(
                {
                    "node": "inbox_prepare",
                    "item": email_key,
                    "error": str(exc)[:500],
                }
            )

    return preparation


def _mark_applied(
    ctx: AgentContext,
    email_key: str,
    *,
    candidate_id: str | None = None,
    draft_type: str | None = None,
) -> None:
    stage = read_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, email_key) or {}
    minimal = {
        "email_key": email_key,
        "kind": stage.get("kind"),
        "uid": stage.get("uid"),
        "message_id": stage.get("message_id"),
        "applied": True,
        "candidate_id": candidate_id,
        "draft_type": draft_type,
    }
    write_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, email_key, minimal)


def _existing_outcome(
    ctx: AgentContext, email_key: str, stage: dict[str, Any], log
) -> InboxApplyOutcome:  # noqa: ANN001
    candidate_id = log.candidate_id
    draft_type = None
    if candidate_id:
        with suppress(Exception):
            candidate = ctx.services.candidates.get(candidate_id)
            draft_type = (
                DraftType.RESUBMISSION_REQUEST
                if candidate.status == CandidateStatus.NEEDS_RESUBMISSION
                else DraftType.ACKNOWLEDGEMENT
            )
    with suppress(Exception):
        ctx.imap.mark_processed(stage["uid"])
    _mark_applied(ctx, email_key, candidate_id=candidate_id, draft_type=draft_type)
    return InboxApplyOutcome(
        counts={"skipped": 1},
        candidate_id=candidate_id,
        draft_type=draft_type,
        skipped=[{"email_key": email_key, "reason": "Email already processed"}],
    )


def apply_inbox_application(
    ctx: AgentContext, item: InboxApplicationInput
) -> InboxApplyOutcome:
    """Apply one classification/job-match result. Safe to repeat."""
    outcome = InboxApplyOutcome()
    stage = read_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, item.email_key)
    if stage is None:
        outcome.counts["failed"] = 1
        outcome.errors.append(
            {"node": "inbox_apply", "item": item.email_key, "error": "Unknown email_key"}
        )
        return outcome
    if stage.get("applied"):
        return InboxApplyOutcome(
            counts={"skipped": 1},
            candidate_id=stage.get("candidate_id"),
            draft_type=stage.get("draft_type"),
            skipped=[{"email_key": item.email_key, "reason": "Already applied"}],
        )

    message_id = stage["message_id"]
    existing = ctx.services.emails.find_inbound(message_id)
    if existing is not None:
        return _existing_outcome(ctx, item.email_key, stage, existing)

    ctx.services.audit.record(
        actor=ctx.actor,
        action="inbox.classification",
        entity_type="email",
        entity_id=item.email_key,
        new_value={
            "is_application": item.classification.is_application,
            "confidence": item.classification.confidence,
        },
        model_name=item.classification_model,
        prompt_version=item.classification_prompt_version,
        run_id=ctx.run_id,
    )
    ctx.services.audit.session.commit()

    if not item.classification.is_application or item.classification.confidence < 0.5:
        ctx.services.emails.record_inbound(
            message_id=message_id,
            from_address=_address(stage["from_address"]),
            to_address=_address(stage["to_address"]),
            subject=stage["subject"],
            status=EmailLogStatus.SKIPPED,
            run_id=ctx.run_id,
        )
        with suppress(Exception):
            ctx.imap.mark_processed(stage["uid"])
        _mark_applied(ctx, item.email_key)
        outcome.counts["skipped"] = 1
        outcome.skipped.append(
            {"email_key": item.email_key, "reason": "Not a job application"}
        )
        return outcome

    job_id = stage.get("job_id")
    if item.job_match is not None:
        matched = (
            item.job_match.job_id in set(stage.get("open_job_ids") or [])
            and item.job_match.confidence >= 0.6
        )
        if matched:
            job_id = item.job_match.job_id
        ctx.services.audit.record(
            actor=ctx.actor,
            action="inbox.job_match",
            entity_type="email",
            entity_id=item.email_key,
            new_value={
                "job_id": job_id,
                "confidence": item.job_match.confidence,
                "accepted": matched,
            },
            model_name=item.job_match_model,
            prompt_version=item.job_match_prompt_version,
            run_id=ctx.run_id,
        )
        ctx.services.audit.session.commit()

    sender = _address(stage["from_address"])
    if not sender:
        ctx.services.emails.record_inbound(
            message_id=message_id,
            from_address="",
            to_address=_address(stage["to_address"]),
            subject=stage["subject"],
            status=EmailLogStatus.FAILED,
            run_id=ctx.run_id,
            error="The email has no usable sender address",
        )
        outcome.counts["failed"] = 1
        outcome.errors.append(
            {
                "node": "inbox_apply",
                "item": item.email_key,
                "error": "The email has no usable sender address",
            }
        )
        return outcome

    candidate = ctx.services.candidates.find_by_source_email(item.email_key)
    created = False
    if candidate is None:
        duplicate = ctx.services.candidates.find_duplicate(sender, job_id)
        if duplicate is not None:
            ctx.services.emails.record_inbound(
                message_id=message_id,
                from_address=sender,
                to_address=_address(stage["to_address"]),
                subject=stage["subject"],
                status=EmailLogStatus.SKIPPED,
                run_id=ctx.run_id,
                candidate_id=duplicate.id,
            )
            with suppress(Exception):
                ctx.imap.mark_processed(stage["uid"])
            _mark_applied(ctx, item.email_key, candidate_id=duplicate.id)
            outcome.counts["skipped"] = 1
            outcome.candidate_id = duplicate.id
            outcome.skipped.append(
                {"email_key": item.email_key, "reason": "Duplicate application"}
            )
            return outcome

        try:
            candidate = ctx.services.candidates.create_from_application(
                ApplicationInput(
                    full_name=_guess_name(stage.get("display_name") or "", "", sender),
                    email=sender,
                    phone=stage.get("phone"),
                    job_id=job_id,
                    source_email_id=item.email_key,
                    consent_given=False,
                    consent_source="inbound_email",
                ),
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            created = True
        except DuplicateCandidateError:
            candidate = ctx.services.candidates.find_by_source_email(item.email_key)
            if candidate is None:
                duplicate = ctx.services.candidates.find_duplicate(sender, job_id)
                if duplicate is None:
                    outcome.counts["failed"] = 1
                    outcome.errors.append(
                        {
                            "node": "inbox_apply",
                            "item": item.email_key,
                            "error": "Candidate creation conflicted with another request",
                        }
                    )
                    return outcome
                candidate = duplicate
                _mark_applied(ctx, item.email_key, candidate_id=candidate.id)
                outcome.counts["skipped"] = 1
                outcome.candidate_id = candidate.id
                return outcome

    resume_ok = bool(candidate.resume_text) and candidate.resume_parse_method != "failed"
    attachments = stage.get("attachments") or []
    if not resume_ok and attachments:
        meta = AttachmentMeta(**attachments[0])
        suffix = Path(meta.filename).suffix.lower()
        tmp_dir = ctx.storage.root / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{stage['uid']}{suffix}"
        try:
            ctx.imap.download_attachment(stage["uid"], meta, tmp_path)
            data = tmp_path.read_bytes()
            stored = ctx.storage.save(data, meta.filename, candidate.id)
            parsed = parse_resume(
                ctx.storage.resolve(stored.relative_path),
                min_chars=ctx.settings.resume_min_chars,
            )
            name = _guess_name(stage.get("display_name") or "", parsed.text, sender)
            ctx.services.candidates.attach_resume(
                candidate.id,
                relative_path=stored.relative_path,
                text=parsed.text,
                method=parsed.method,
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            if name != candidate.full_name:
                ctx.services.candidates.set_full_name(candidate.id, name, actor=ctx.actor)
            resume_ok = parsed.method != "failed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("resume_attachment_failed", error=str(exc)[:200])
            ctx.services.candidates.add_error_note(
                candidate.id, f"Attachment could not be stored: {exc}"[:500], ctx.actor
            )
            resume_ok = False
        finally:
            tmp_path.unlink(missing_ok=True)

    if resume_ok:
        if job_id is None and candidate.status == CandidateStatus.RECEIVED:
            ctx.services.candidates.transition(
                candidate.id,
                CandidateStatus.NEEDS_REVIEW,
                actor=ctx.actor,
                reason="No matching open job",
                run_id=ctx.run_id,
            )
        draft_type = DraftType.ACKNOWLEDGEMENT
    else:
        if candidate.status == CandidateStatus.RECEIVED:
            ctx.services.candidates.transition(
                candidate.id,
                CandidateStatus.NEEDS_RESUBMISSION,
                actor=ctx.actor,
                reason="Resume missing or unreadable",
                run_id=ctx.run_id,
            )
        draft_type = DraftType.RESUBMISSION_REQUEST

    if ctx.services.emails.find_inbound(message_id) is None:
        ctx.services.emails.record_inbound(
            message_id=message_id,
            from_address=sender,
            to_address=_address(stage["to_address"]),
            subject=stage["subject"],
            status=EmailLogStatus.PROCESSED,
            run_id=ctx.run_id,
            candidate_id=candidate.id,
        )
    with suppress(Exception):
        ctx.imap.mark_processed(stage["uid"])

    _mark_applied(ctx, item.email_key, candidate_id=candidate.id, draft_type=draft_type)
    outcome.counts["created" if created else "applied"] = 1
    outcome.candidate_id = candidate.id
    outcome.draft_type = draft_type
    return outcome


def apply_inbox_reply(ctx: AgentContext, item: InboxReplyInput) -> InboxApplyOutcome:
    """Apply one reply classification. Safe to repeat."""
    outcome = InboxApplyOutcome()
    stage = read_stage(ctx.settings.upload_dir, STAGE_NAMESPACE, item.email_key)
    if stage is None:
        outcome.counts["failed"] = 1
        outcome.errors.append(
            {"node": "inbox_reply", "item": item.email_key, "error": "Unknown email_key"}
        )
        return outcome
    if stage.get("applied"):
        return InboxApplyOutcome(
            counts={"skipped": 1},
            candidate_id=stage.get("candidate_id"),
            skipped=[{"email_key": item.email_key, "reason": "Already applied"}],
        )

    message_id = stage["message_id"]
    existing = ctx.services.emails.find_inbound(message_id)
    if existing is not None:
        return _existing_outcome(ctx, item.email_key, stage, existing)

    candidate_id = stage.get("candidate_id")
    try:
        ctx.services.candidates.get(candidate_id)
    except Exception as exc:  # noqa: BLE001
        outcome.counts["failed"] = 1
        outcome.errors.append(
            {"node": "inbox_reply", "item": item.email_key, "error": str(exc)[:500]}
        )
        return outcome

    try:
        ctx.services.replies.record(
            candidate_id=candidate_id,
            category=ReplyCategory(item.reply.category),
            actor=ctx.actor,
            confidence=item.reply.confidence,
            body_excerpt=stage.get("body_excerpt"),
            model_name=item.model_name,
            prompt_version=item.prompt_version,
            run_id=ctx.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        outcome.counts["failed"] = 1
        outcome.errors.append(
            {"node": "inbox_reply", "item": item.email_key, "error": str(exc)[:500]}
        )
        return outcome

    ctx.services.emails.record_inbound(
        message_id=message_id,
        from_address=_address(stage.get("from_address", "")),
        to_address=_address(stage.get("to_address", "")),
        subject=stage.get("subject", ""),
        status=EmailLogStatus.PROCESSED,
        run_id=ctx.run_id,
        candidate_id=candidate_id,
    )
    with suppress(Exception):
        ctx.imap.mark_processed(stage["uid"])
    _mark_applied(ctx, item.email_key, candidate_id=candidate_id)
    outcome.counts["replies"] = 1
    outcome.candidate_id = candidate_id
    return outcome


def acknowledge_candidate(
    ctx: AgentContext, *, candidate_id: str, draft_type: str | None
) -> AcknowledgeOutcome:
    """Create the deterministic acknowledgement or resubmission draft once."""
    outcome = AcknowledgeOutcome()
    if not draft_type:
        # The apply step returns no draft type for duplicates and skips. The
        # flow calls this endpoint unconditionally, so a null means "nothing
        # to acknowledge".
        outcome.skipped = True
        return outcome
    if draft_type not in {
        DraftType.ACKNOWLEDGEMENT,
        DraftType.RESUBMISSION_REQUEST,
    }:
        outcome.errors.append(
            {
                "node": "acknowledge",
                "item": candidate_id,
                "error": f"Unknown draft type: {draft_type}",
            }
        )
        return outcome

    try:
        candidate = ctx.services.candidates.get(candidate_id)
    except Exception as exc:  # noqa: BLE001
        outcome.errors.append(
            {"node": "acknowledge", "item": candidate_id, "error": str(exc)[:500]}
        )
        return outcome

    for existing_type in (DraftType.ACKNOWLEDGEMENT, DraftType.RESUBMISSION_REQUEST):
        if ctx.services.drafts.active_for(candidate.id, existing_type) is not None:
            outcome.skipped = True
            return outcome

    job = None
    if candidate.job_id:
        with suppress(Exception):
            job = ctx.services.jobs.get(candidate.job_id)
    subject, body = _draft_templates(
        job.title if job else "the advertised role", ctx.settings.retention_days
    )[draft_type]
    try:
        draft = ctx.services.drafts.create_draft(
            candidate_id=candidate.id,
            draft_type=draft_type,
            subject=subject,
            body=body,
            actor=ctx.actor,
            run_id=ctx.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        outcome.errors.append(
            {"node": "acknowledge", "item": candidate.id, "error": str(exc)[:500]}
        )
        return outcome

    outcome.created = True
    outcome.draft_id = draft.id
    return outcome


__all__ = [
    "AcknowledgeOutcome",
    "InboxApplicationInput",
    "InboxApplicationItem",
    "InboxApplyOutcome",
    "InboxPreparation",
    "InboxReplyInput",
    "InboxReplyItem",
    "PromptBlock",
    "acknowledge_candidate",
    "apply_inbox_application",
    "apply_inbox_reply",
    "prepare_inbox",
]
