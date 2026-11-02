"""HR agent: send only approved drafts, then record the decision.

This is the single send path (R6). Anything not approved is never mailed.
"""

from __future__ import annotations

import structlog

from app.agents.contracts import AgentContext, AgentResult
from app.core.constants import CandidateStatus, DraftType, EmailLogStatus
from app.utils.idempotency import claim_marker, marker_path, release_marker
from app.utils.mailer import OutboundEmail

logger = structlog.get_logger(__name__)

AGENT = "hr_agent"

TARGET_STATUS = {
    DraftType.INTERVIEW_INVITE: CandidateStatus.ACCEPTED,
    DraftType.REJECTION: CandidateStatus.REJECTED,
}


def _finalize_candidate(ctx: AgentContext, draft, errors: list[dict]) -> None:  # noqa: ANN001
    """Move the candidate to its final status once the draft is sent.

    A retry may find the draft already sent but the status transition missing,
    so this runs from both the normal and the recovery paths.
    """
    target = TARGET_STATUS.get(draft.draft_type)
    if target is None:
        return
    try:
        candidate = ctx.services.candidates.get(draft.candidate_id)
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "node": "send_decisions",
                "item": draft.candidate_id,
                "error": f"email sent but candidate lookup failed: {exc}"[:500],
            }
        )
        return
    if candidate.status == target:
        return
    try:
        ctx.services.candidates.transition(
            candidate.id,
            target,
            actor=ctx.actor,
            reason=f"Decision email sent ({draft.draft_type})",
            run_id=ctx.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "node": "send_decisions",
                "item": candidate.id,
                "error": f"email sent but status update failed: {exc}"[:500],
            }
        )


def send_approved(
    ctx: AgentContext, *, draft_ids: list[str] | None = None
) -> AgentResult:
    counts = {"sent": 0, "skipped": 0}
    errors: list[dict] = []

    if draft_ids:
        # Validate the whole batch before sending anything. An unapproved draft
        # raises ApprovalRequiredError (R6), so no email leaves in that request.
        # A draft that is already sent is kept in the list and skipped below, so
        # an explicit-id retry stays idempotent.
        drafts = []
        for draft_id in draft_ids:
            current = ctx.services.drafts.get(draft_id)
            if current.sent_at is not None:
                drafts.append(current)
            else:
                drafts.append(ctx.services.drafts.assert_sendable(draft_id))
    else:
        drafts = ctx.services.drafts.list_approved(run_id=ctx.run_id)

    for draft in drafts:
        if draft.sent_at is not None:
            _finalize_candidate(ctx, draft, errors)
            counts["skipped"] += 1
            continue

        marker = marker_path(ctx.storage.root, f"send:{draft.id}")
        if marker.exists():
            # A previous attempt sent this draft but may have failed before
            # mark_sent. Finish the local state without mailing again.
            ctx.services.drafts.mark_sent(draft.id, actor=ctx.actor)
            _finalize_candidate(ctx, draft, errors)
            counts["skipped"] += 1
            continue
        if not claim_marker(marker):
            counts["skipped"] += 1
            continue

        candidate = ctx.services.candidates.get(draft.candidate_id)
        try:
            result = ctx.mailer.send(
                OutboundEmail(
                    to=[candidate.email],
                    subject=draft.subject,
                    body_text=draft.body,
                )
            )
        except Exception as exc:  # noqa: BLE001
            release_marker(marker)
            logger.error("candidate_email_failed", draft_id=draft.id, error=str(exc)[:300])
            ctx.services.drafts.mark_failed(draft.id, error=str(exc), actor=ctx.actor)
            ctx.services.emails.record_outbound(
                to_address=candidate.email,
                subject=draft.subject,
                status=EmailLogStatus.FAILED,
                actor=ctx.actor,
                run_id=ctx.run_id,
                candidate_id=candidate.id,
                draft_id=draft.id,
                error=str(exc)[:1000],
            )
            errors.append(
                {"node": "send_decisions", "item": draft.id, "error": str(exc)[:500]}
            )
            continue

        ctx.services.emails.record_outbound(
            to_address=candidate.email,
            subject=draft.subject,
            status=EmailLogStatus.DRY_RUN if result.dry_run else EmailLogStatus.SENT,
            actor=ctx.actor,
            run_id=ctx.run_id,
            candidate_id=candidate.id,
            draft_id=draft.id,
            message_id=result.message_id,
            from_address=ctx.settings.smtp_from or ctx.settings.smtp_user,
        )
        ctx.services.drafts.mark_sent(draft.id, actor=ctx.actor)
        _finalize_candidate(ctx, draft, errors)
        counts["sent"] += 1

    return AgentResult(counts=counts, errors=errors)
