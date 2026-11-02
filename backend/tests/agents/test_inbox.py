from __future__ import annotations

from app.agents.hr_agent.inbox import read_inbox
from app.core.constants import CandidateStatus, DraftType
from app.db.models.candidate import Candidate
from app.db.models.candidate_reply import CandidateReply
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from app.schemas.llm import EmailClassification, JobMatch, ReplyClassification
from sqlalchemy.orm import Session
from tests.agents.conftest import make_email, make_pdf, resume_attachment
from tests.fakes import FakeImapClient

RESUME_PDF = make_pdf(
    "Priya Sharma",
    "Backend engineer with five years of Python experience.",
    "Skills: Python, SQL, FastAPI, PostgreSQL.",
)


def _application_llm(ctx, job_id: str | None = None) -> None:
    ctx.llm.responses = {
        "EmailClassification": EmailClassification(
            is_application=True, confidence=0.95, reason="resume attached"
        ),
        "JobMatch": JobMatch(job_id=job_id, confidence=0.9 if job_id else 0.1, rationale="skills"),
    }


def test_creates_candidate_with_parsed_resume(ctx, session: Session, job):
    _application_llm(ctx, job.id)
    ctx.imap = FakeImapClient(
        [make_email(attachments=[resume_attachment()])],
        attachment_bytes=RESUME_PDF,
    )

    result = read_inbox(ctx)

    candidate = session.query(Candidate).one()
    session.refresh(candidate)
    assert candidate.email == "priya@example.com"
    assert candidate.full_name == "Priya Sharma"
    assert candidate.job_id == job.id
    assert candidate.status == CandidateStatus.RECEIVED
    assert "Python" in (candidate.resume_text or "")
    assert candidate.resume_parse_method == "pdfplumber"
    assert result.counts["created"] == 1
    assert result.counts["fetched"] == 1

    draft = session.query(EmailDraft).one()
    assert draft.draft_type == DraftType.ACKNOWLEDGEMENT
    assert draft.approved is False
    assert "Privacy notice" in draft.body

    log = session.query(EmailLog).one()
    assert log.status == "processed"
    assert log.candidate_id == candidate.id
    assert ctx.imap.marked == ["1"]


def test_llm_receives_redacted_text(ctx, session: Session, job):
    _application_llm(ctx, job.id)
    ctx.imap = FakeImapClient(
        [
            make_email(
                body=(
                    "Please find my resume attached. Reach me at priya@example.com "
                    "or +91 98765 43210. I am Priya Sharma."
                ),
                attachments=[resume_attachment()],
            )
        ],
        attachment_bytes=RESUME_PDF,
    )

    read_inbox(ctx)

    classification_call = next(
        call for call in ctx.llm.calls if call["schema"] == "EmailClassification"
    )
    assert "priya@example.com" not in classification_call["user"]
    assert "98765" not in classification_call["user"]
    assert "Priya Sharma" not in classification_call["user"]


def test_unreadable_resume_asks_for_resubmission(ctx, session: Session, job):
    _application_llm(ctx, job.id)
    ctx.imap = FakeImapClient(
        [make_email(attachments=[resume_attachment()])],
        attachment_bytes=b"%PDF-1.4 this is not a readable pdf",
    )

    result = read_inbox(ctx)

    candidate = session.query(Candidate).one()
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.NEEDS_RESUBMISSION
    draft = session.query(EmailDraft).one()
    assert draft.draft_type == DraftType.RESUBMISSION_REQUEST
    assert result.counts["needs_resubmission"] == 1


def test_missing_attachment_asks_for_resubmission(ctx, session: Session, job):
    _application_llm(ctx, job.id)
    ctx.imap = FakeImapClient([make_email(attachments=[])])

    result = read_inbox(ctx)

    candidate = session.query(Candidate).one()
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.NEEDS_RESUBMISSION
    assert result.counts["needs_resubmission"] == 1


def test_non_application_is_skipped(ctx, session: Session):
    ctx.llm.responses = {
        "EmailClassification": EmailClassification(
            is_application=False, confidence=0.98, reason="newsletter"
        )
    }
    ctx.imap = FakeImapClient([make_email(subject="October newsletter", body="Offers inside")])

    result = read_inbox(ctx)

    assert session.query(Candidate).count() == 0
    assert result.counts["skipped"] == 1
    assert ctx.imap.marked == ["1"]


def test_duplicate_message_id_is_skipped(ctx, session: Session):
    session.add(
        EmailLog(
            direction="inbound",
            message_id="<in-1@test>",
            from_address="priya@example.com",
            to_address="hr@example.com",
            subject="Application",
            status="processed",
        )
    )
    session.commit()
    ctx.imap = FakeImapClient([make_email()])

    result = read_inbox(ctx)

    assert session.query(Candidate).count() == 0
    assert result.counts["duplicates"] == 1
    assert ctx.imap.marked == ["1"]


def _existing_candidate(session: Session, job) -> Candidate:
    candidate = Candidate(
        full_name="Priya Sharma",
        email="priya@example.com",
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="Python developer.",
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def test_reply_is_classified_and_recorded(ctx, session: Session, job):
    candidate = _existing_candidate(session, job)
    session.add(
        EmailLog(
            direction="outbound",
            message_id="<out-1@test>",
            from_address="hr@example.com",
            to_address="priya@example.com",
            subject="Interview",
            status="sent",
        )
    )
    session.commit()
    ctx.llm.responses = {
        "ReplyClassification": ReplyClassification(category="question", confidence=0.88)
    }
    ctx.imap = FakeImapClient(
        [
            make_email(
                uid="9",
                message_id="<reply-1@test>",
                subject="Re: Interview",
                body="What time is the interview?",
                in_reply_to="<out-1@test>",
            )
        ]
    )

    result = read_inbox(ctx)

    assert session.query(Candidate).count() == 1
    reply = session.query(CandidateReply).one()
    assert reply.candidate_id == candidate.id
    assert reply.category == "question"
    assert result.counts["replies"] == 1
    assert ctx.imap.marked == ["9"]


def test_withdrawal_reply_updates_candidate(ctx, session: Session, job):
    candidate = _existing_candidate(session, job)
    ctx.llm.responses = {
        "ReplyClassification": ReplyClassification(category="withdrawn", confidence=0.95)
    }
    ctx.imap = FakeImapClient(
        [
            make_email(
                uid="10",
                message_id="<reply-2@test>",
                subject="Re: Application",
                body="I have accepted another offer.",
            )
        ]
    )

    read_inbox(ctx)

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.WITHDRAWN
    assert candidate.deletion_requested is True


def test_job_alias_match_does_not_call_the_llm(ctx, session: Session, job):
    job.email_alias = "backend@example.com"
    session.commit()
    ctx.llm.responses = {
        "EmailClassification": EmailClassification(
            is_application=True, confidence=0.9, reason="resume"
        )
    }
    ctx.imap = FakeImapClient(
        [
            make_email(
                to="backend@example.com",
                subject="Application",
                attachments=[resume_attachment()],
            )
        ],
        attachment_bytes=RESUME_PDF,
    )

    read_inbox(ctx)

    candidate = session.query(Candidate).one()
    assert candidate.job_id == job.id
    assert all(call["schema"] != "JobMatch" for call in ctx.llm.calls)


def test_unmatched_application_goes_to_needs_review(ctx, session: Session, job):
    ctx.llm.responses = {
        "EmailClassification": EmailClassification(
            is_application=True, confidence=0.9, reason="resume"
        ),
        "JobMatch": JobMatch(job_id=None, confidence=0.2, rationale="unclear"),
    }
    ctx.imap = FakeImapClient(
        [make_email(attachments=[resume_attachment()])], attachment_bytes=RESUME_PDF
    )

    result = read_inbox(ctx)

    candidate = session.query(Candidate).one()
    session.refresh(candidate)
    assert candidate.job_id is None
    assert candidate.status == CandidateStatus.NEEDS_REVIEW
    assert result.counts["needs_review"] == 1


def test_llm_failure_is_recorded_and_message_left_unread(ctx, session: Session):
    ctx.llm.fail_times = 1
    ctx.imap = FakeImapClient([make_email()])

    result = read_inbox(ctx)

    assert result.counts["failed"] == 1
    assert result.errors[0]["node"] == "read_emails"
    assert session.query(Candidate).count() == 0
    assert ctx.imap.marked == []
    log = session.query(EmailLog).one()
    assert log.status == "failed"


def test_imap_failure_returns_alert(ctx):
    class BrokenImap(FakeImapClient):
        def fetch_unread(self, limit: int = 50):
            raise RuntimeError("mailbox down")

    ctx.imap = BrokenImap()
    result = read_inbox(ctx)
    assert result.counts["fetched"] == 0
    assert result.errors[0]["node"] == "read_emails"
