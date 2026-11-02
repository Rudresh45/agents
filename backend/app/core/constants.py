"""Domain constants: enums and the frozen status machine (R11)."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    HR = "hr"
    HR_MANAGER = "hr_manager"
    CEO = "ceo"


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CandidateStatus(StrEnum):
    RECEIVED = "received"
    NEEDS_REVIEW = "needs_review"
    NEEDS_RESUBMISSION = "needs_resubmission"
    FORWARDED = "forwarded"
    SHORTLISTED = "shortlisted"
    NOT_SHORTLISTED = "not_shortlisted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ANONYMISED = "anonymised"
    DELETED = "deleted"


class DraftType(StrEnum):
    INTERVIEW_INVITE = "interview_invite"
    REJECTION = "rejection"
    RESUBMISSION_REQUEST = "resubmission_request"
    ACKNOWLEDGEMENT = "acknowledgement"


class ReplyCategory(StrEnum):
    ACCEPTS = "accepts"
    DECLINES = "declines"
    RESCHEDULE = "reschedule"
    QUESTION = "question"
    WITHDRAWN = "withdrawn"
    OTHER = "other"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EmailDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class EmailLogStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    SKIPPED = "skipped"
    SENT = "sent"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class DataRequestType(StrEnum):
    ACCESS = "access"
    DELETION = "deletion"


class DataRequestStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    REJECTED = "rejected"


class ReportStatus(StrEnum):
    GENERATED = "generated"
    SENT = "sent"
    FAILED = "failed"


ACTIVE_RUN_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.WAITING_APPROVAL})
TERMINAL_CANDIDATE_STATUSES = frozenset(
    {
        CandidateStatus.ACCEPTED,
        CandidateStatus.REJECTED,
        CandidateStatus.WITHDRAWN,
        CandidateStatus.ANONYMISED,
        CandidateStatus.DELETED,
    }
)

# The only legal transitions (spec section 7). New statuses may be added later;
# existing pairs are never removed or reordered (R11).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    CandidateStatus.RECEIVED: frozenset(
        {
            CandidateStatus.NEEDS_REVIEW,
            CandidateStatus.NEEDS_RESUBMISSION,
            CandidateStatus.FORWARDED,
        }
    ),
    CandidateStatus.NEEDS_REVIEW: frozenset({CandidateStatus.RECEIVED}),
    CandidateStatus.NEEDS_RESUBMISSION: frozenset({CandidateStatus.RECEIVED}),
    CandidateStatus.FORWARDED: frozenset(
        {CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED, CandidateStatus.NEEDS_REVIEW}
    ),
    CandidateStatus.SHORTLISTED: frozenset(
        {CandidateStatus.NOT_SHORTLISTED, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}
    ),
    CandidateStatus.NOT_SHORTLISTED: frozenset(
        {CandidateStatus.SHORTLISTED, CandidateStatus.REJECTED, CandidateStatus.ACCEPTED}
    ),
    CandidateStatus.ACCEPTED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
    CandidateStatus.WITHDRAWN: frozenset(),
    CandidateStatus.ANONYMISED: frozenset(),
    CandidateStatus.DELETED: frozenset(),
}

# Transitions HR may make by hand, but only with a written reason.
OVERRIDE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED),
        (CandidateStatus.NOT_SHORTLISTED, CandidateStatus.SHORTLISTED),
    }
)

# Side exits available from any non-deleted state (spec section 7).
SIDE_EXITS: frozenset[str] = frozenset(
    {CandidateStatus.WITHDRAWN, CandidateStatus.ANONYMISED, CandidateStatus.DELETED}
)

# Which draft type is required to reach a final status.
FINAL_STATUS_DRAFT_TYPE: dict[str, str] = {
    CandidateStatus.ACCEPTED: DraftType.INTERVIEW_INVITE,
    CandidateStatus.REJECTED: DraftType.REJECTION,
}


def is_transition_allowed(old_status: str, new_status: str) -> bool:
    if new_status == CandidateStatus.DELETED:
        return old_status != CandidateStatus.DELETED
    if new_status == CandidateStatus.ANONYMISED:
        return old_status not in {CandidateStatus.ANONYMISED, CandidateStatus.DELETED}
    if new_status == CandidateStatus.WITHDRAWN:
        return old_status not in {
            CandidateStatus.WITHDRAWN,
            CandidateStatus.ANONYMISED,
            CandidateStatus.DELETED,
        }
    return new_status in ALLOWED_TRANSITIONS.get(old_status, frozenset())
