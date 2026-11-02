"""HR agent: read the inbox, classify mail, create candidates, file replies.

Privacy rule R9: text sent to the LLM is redacted first. Contact details and the
applicant's name are extracted deterministically and never leave the system.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import suppress
from email.utils import parseaddr
from pathlib import Path

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.agents.prompts import get_prompt
from app.core.constants import CandidateStatus, DraftType, EmailLogStatus, ReplyCategory
from app.schemas.domain import ApplicationInput
from app.schemas.llm import EmailClassification, JobMatch, ReplyClassification
from app.utils.imap_client import InboundEmail
from app.utils.redaction import redact_pii
from app.utils.resume_parser import parse_resume

logger = structlog.get_logger(__name__)

AGENT = "hr_agent"
PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,79}$")
MAX_BODY_CHARS = 6000


def _address(value: str) -> str:
    return parseaddr(value or "")[1].strip().lower()


def _display_name(value: str) -> str:
    name = parseaddr(value or "")[0].strip().strip('"')
    if name and "@" not in name and NAME_RE.match(name):
        return name
    return ""


def _phone(text: str) -> str | None:
    for match in PHONE_RE.finditer(text or ""):
        value = match.group(0).strip()
        if sum(char.isdigit() for char in value) >= 10:
            return value
    return None


def _guess_name(display_name: str, resume_text: str, email: str) -> str:
    if display_name:
        return display_name
    for line in (resume_text or "").splitlines()[:6]:
        candidate = line.strip()
        if NAME_RE.match(candidate) and len(candidate.split()) <= 5:
            return candidate
    local = email.split("@")[0]
    return local.replace(".", " ").replace("_", " ").title() or "Candidate"


def _role_from_subject(subject: str) -> str | None:
    patterns = [
        r"application\s+for\s+(?:the\s+)?(.+)",
        r"applying\s+for\s+(?:the\s+)?(.+)",
        r"application\s*[-:]\s*(.+)",
        r"resume\s+for\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, subject or "", re.IGNORECASE)
        if match:
            value = re.split(r"[-|(]", match.group(1))[0].strip(" .")
            if value:
                return value[:200]
    return None


def _email_prompt(email: InboundEmail, body: str) -> str:
    suffixes = sorted(
        {Path(item.filename).suffix.lower().lstrip(".") or "file" for item in email.attachments}
    )
    return (
        f"Subject: {email.subject}\n"
        f"Attachments: {', '.join(suffixes) or 'none'}\n\n"
        f"<email>\n{body[:MAX_BODY_CHARS]}\n</email>"
    )


def _is_reply(ctx: AgentContext, email: InboundEmail) -> bool:
    references = [email.in_reply_to, *email.references]
    for reference in references:
        if reference and ctx.services.emails.exists_message_id(
            reference.strip("<>"), direction="outbound"
        ):
            return True
    sender = _address(email.from_address)
    return bool(sender) and ctx.services.candidates.find_by_email(sender) is not None


def _log(
    ctx: AgentContext,
    email: InboundEmail,
    *,
    status: str,
    candidate_id: str | None = None,
    error: str | None = None,
) -> None:
    ctx.services.emails.record_inbound(
        message_id=email.message_id or f"uid:{email.uid}",
        from_address=_address(email.from_address),
        to_address=_address(email.to_address),
        subject=email.subject,
        status=status,
        run_id=ctx.run_id,
        candidate_id=candidate_id,
        error=error,
    )


def _match_job(ctx: AgentContext, email: InboundEmail, body: str) -> str | None:
    jobs = ctx.services.jobs.open_jobs()
    if not jobs:
        return None

    haystack = f"{email.to_address} {email.subject}".lower()
    for job in jobs:
        if job.email_alias and job.email_alias.lower() in haystack:
            return job.id

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
    match = ctx.llm.invoke(
        JobMatch, system=prompt.full_system, user=user, prompt_version=prompt.version
    )
    valid_ids = {job.id for job in jobs}
    if match.job_id in valid_ids and match.confidence >= 0.6:
        return match.job_id
    return None


def _store_attachment(ctx: AgentContext, email: InboundEmail, candidate_id: str):
    meta = email.attachments[0]
    suffix = Path(meta.filename).suffix.lower()
    tmp_dir = ctx.storage.root / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{email.uid}{suffix}"
    try:
        ctx.imap.download_attachment(email.uid, meta, tmp_path)
        data = tmp_path.read_bytes()
        stored = ctx.storage.save(data, meta.filename, candidate_id)
    finally:
        tmp_path.unlink(missing_ok=True)
    return stored


def _draft_templates(role: str, retention_days: int) -> dict[str, tuple[str, str]]:
    return {
        DraftType.ACKNOWLEDGEMENT: (
            f"Application received - {role}",
            (
                "Dear Candidate,\n\n"
                f"Thank you for applying for the {role} role. We have received your "
                "application and will review it.\n\n"
                "Privacy notice: your details are used only for recruitment, kept for up "
                f"to {retention_days} days, and can be deleted on request by replying to "
                "this email.\n\nKind regards,\nHR Team"
            ),
        ),
        DraftType.RESUBMISSION_REQUEST: (
            f"We could not read your resume - {role}",
            (
                "Dear Candidate,\n\n"
                f"Thank you for applying for the {role} role. The resume file we received "
                "could not be read. Please reply with your resume attached as a PDF or DOCX "
                "file so we can review your application.\n\n"
                "Privacy notice: your details are used only for recruitment, kept for up "
                f"to {retention_days} days, and can be deleted on request.\n\n"
                "Kind regards,\nHR Team"
            ),
        ),
    }


def _create_follow_up_draft(
    ctx: AgentContext,
    *,
    candidate_id: str,
    job_title: str | None,
    draft_type: str,
) -> None:
    role = job_title or "the advertised role"
    subject, body = _draft_templates(role, ctx.settings.retention_days)[draft_type]
    ctx.services.drafts.create_draft(
        candidate_id=candidate_id,
        draft_type=draft_type,
        subject=subject,
        body=body,
        actor=ctx.actor,
        run_id=ctx.run_id,
    )


def _handle_reply(ctx: AgentContext, email: InboundEmail, counts: Counter) -> None:
    candidate = ctx.services.candidates.find_by_email(_address(email.from_address))
    if candidate is None:
        _log(ctx, email, status=EmailLogStatus.SKIPPED)
        ctx.imap.mark_processed(email.uid)
        counts["skipped"] += 1
        return

    redacted = redact_pii(email.body_text, known_name=candidate.full_name)
    prompt = get_prompt("classify_reply")
    category = ReplyCategory.OTHER
    confidence: float | None = None
    try:
        result = ctx.llm.invoke(
            ReplyClassification,
            system=prompt.full_system,
            user=_email_prompt(email, redacted.text),
            prompt_version=prompt.version,
        )
        category = ReplyCategory(result.category)
        confidence = result.confidence
    except Exception as exc:  # noqa: BLE001
        logger.warning("reply_classification_failed", error=str(exc)[:200])

    ctx.services.replies.record(
        candidate_id=candidate.id,
        category=category,
        actor=ctx.actor,
        confidence=confidence,
        body_excerpt=redacted.text[:1000],
    )
    _log(ctx, email, status=EmailLogStatus.PROCESSED, candidate_id=candidate.id)
    ctx.imap.mark_processed(email.uid)
    counts["replies"] += 1


def _handle_application(ctx: AgentContext, email: InboundEmail, counts: Counter) -> None:
    redacted = redact_pii(email.body_text, known_name=_display_name(email.from_address) or None)
    prompt = get_prompt("classify_email")
    classification = ctx.llm.invoke(
        EmailClassification,
        system=prompt.full_system,
        user=_email_prompt(email, redacted.text),
        prompt_version=prompt.version,
    )
    if not classification.is_application or classification.confidence < 0.5:
        _log(ctx, email, status=EmailLogStatus.SKIPPED)
        ctx.imap.mark_processed(email.uid)
        counts["skipped"] += 1
        return

    sender = _address(email.from_address)
    if not sender:
        raise ValueError("The email has no usable sender address")

    job_id = _match_job(ctx, email, redacted.text)
    if ctx.services.candidates.find_duplicate(sender, job_id):
        _log(ctx, email, status=EmailLogStatus.SKIPPED)
        ctx.imap.mark_processed(email.uid)
        counts["duplicates"] += 1
        return

    candidate = ctx.services.candidates.create_from_application(
        ApplicationInput(
            full_name=_guess_name(
                _display_name(email.from_address), "", sender
            ),
            email=sender,
            phone=_phone(email.body_text),
            job_id=job_id,
            source_email_id=None,
            consent_given=False,
            consent_source="inbound_email",
        ),
        actor=ctx.actor,
        run_id=ctx.run_id,
    )

    resume_ok = False
    if email.attachments:
        try:
            stored = _store_attachment(ctx, email, candidate.id)
            parsed = parse_resume(
                ctx.storage.resolve(stored.relative_path),
                min_chars=ctx.settings.resume_min_chars,
            )
            name = _guess_name(
                _display_name(email.from_address), parsed.text, sender
            )
            ctx.services.candidates.attach_resume(
                candidate.id,
                relative_path=stored.relative_path,
                text=parsed.text,
                method=parsed.method,
                actor=ctx.actor,
                run_id=ctx.run_id,
            )
            if name != candidate.full_name:
                ctx.services.candidates.set_full_name(candidate.id, name)
            resume_ok = parsed.method != "failed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("resume_attachment_failed", error=str(exc)[:200])
            ctx.services.candidates.add_error_note(
                candidate.id, f"Attachment could not be stored: {exc}"[:500], ctx.actor
            )

    if resume_ok:
        job = ctx.services.jobs.get(job_id) if job_id else None
        _create_follow_up_draft(
            ctx,
            candidate_id=candidate.id,
            job_title=job.title if job else None,
            draft_type=DraftType.ACKNOWLEDGEMENT,
        )
        if job_id is None:
            ctx.services.candidates.transition(
                candidate.id,
                CandidateStatus.NEEDS_REVIEW,
                actor=ctx.actor,
                reason="No matching open job",
                run_id=ctx.run_id,
            )
            counts["needs_review"] += 1
        counts["created"] += 1
    else:
        ctx.services.candidates.transition(
            candidate.id,
            CandidateStatus.NEEDS_RESUBMISSION,
            actor=ctx.actor,
            reason="Resume missing or unreadable",
            run_id=ctx.run_id,
        )
        job = ctx.services.jobs.get(job_id) if job_id else None
        _create_follow_up_draft(
            ctx,
            candidate_id=candidate.id,
            job_title=job.title if job else None,
            draft_type=DraftType.RESUBMISSION_REQUEST,
        )
        counts["needs_resubmission"] += 1

    if job_id is None:
        counts["unmatched"] += 1

    _log(ctx, email, status=EmailLogStatus.PROCESSED, candidate_id=candidate.id)
    ctx.imap.mark_processed(email.uid)


def read_inbox(ctx: AgentContext, *, limit: int = 50) -> AgentResult:
    counts: Counter = Counter()
    errors: list[dict] = []

    try:
        emails = ctx.imap.fetch_unread(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("inbox_fetch_failed", error=str(exc)[:300])
        ctx.alerts.send(
            "imap_failed",
            "Inbox could not be read",
            f"The HR agent could not read the mailbox: {exc}",
        )
        return AgentResult(
            counts={"fetched": 0},
            errors=[{"node": "read_emails", "error": str(exc)[:500]}],
        )

    counts["fetched"] = len(emails)

    for email in emails:
        key = email.message_id or f"uid:{email.uid}"
        try:
            if ctx.services.emails.exists_message_id(key, direction="inbound"):
                counts["duplicates"] += 1
                ctx.imap.mark_processed(email.uid)
                continue
            if _is_reply(ctx, email):
                _handle_reply(ctx, email, counts)
            else:
                _handle_application(ctx, email, counts)
        except Exception as exc:  # noqa: BLE001
            logger.exception("inbox_message_failed", uid=email.uid)
            counts["failed"] += 1
            errors.append(
                {
                    "node": "read_emails",
                    "item": email.message_id or email.uid,
                    "error": str(exc)[:500],
                }
            )
            with suppress(Exception):
                _log(ctx, email, status=EmailLogStatus.FAILED, error=str(exc)[:1000])

    return AgentResult(counts=dict(counts), errors=errors)
