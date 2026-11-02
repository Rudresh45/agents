from __future__ import annotations

from io import BytesIO

import pytest
from app.agents.contracts import AgentContext
from app.core.config import Settings
from app.db.models.workflow_run import WorkflowRun
from app.schemas.domain import Actor
from app.utils.imap_client import AttachmentMeta, InboundEmail
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session
from tests.fakes import FakeImapClient, FakeMailer, FakeStructuredLLM


@pytest.fixture()
def ctx(
    session: Session, settings: Settings, actor: Actor, run: WorkflowRun
) -> AgentContext:
    return AgentContext.build(
        session=session,
        llm=FakeStructuredLLM(),
        mailer=FakeMailer(settings),
        imap=FakeImapClient(),
        settings=settings,
        actor=Actor.agent("hr_agent"),
        run_id=run.id,
        dry_run=True,
    )


def make_pdf(*lines: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    y = 760
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 18
    pdf.save()
    return buffer.getvalue()


def make_email(
    *,
    uid: str = "1",
    message_id: str = "<in-1@test>",
    sender: str = "Priya Sharma <priya@example.com>",
    to: str = "hr@example.com",
    subject: str = "Application for Backend Engineer",
    body: str = "Please find my resume attached.",
    attachments: list[AttachmentMeta] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> InboundEmail:
    return InboundEmail(
        uid=uid,
        message_id=message_id,
        from_address=sender,
        to_address=to,
        subject=subject,
        body_text=body,
        attachments=attachments or [],
        in_reply_to=in_reply_to,
        references=references or [],
    )


def resume_attachment(size: int = 1000) -> AttachmentMeta:
    return AttachmentMeta(
        part_index=1,
        filename="Priya_Sharma_Resume.pdf",
        content_type="application/pdf",
        size=size,
    )


def make_candidate(
    session: Session,
    *,
    job_id: str | None = None,
    status: str = "received",
    email: str = "asha@example.com",
    full_name: str = "Asha Rao",
    resume_text: str = "Backend engineer with six years of Python and SQL experience.",
    resume_path: str | None = None,
    score: int | None = None,
    summary: str | None = None,
    reason: str | None = None,
    matched_skills: list[str] | None = None,
    missing_skills: list[str] | None = None,
):
    from app.db.models.candidate import Candidate

    candidate = Candidate(
        job_id=job_id,
        full_name=full_name,
        email=email,
        status=status,
        resume_text=resume_text,
        resume_path=resume_path,
        score=score,
        summary=summary,
        reason=reason,
        matched_skills=matched_skills or [],
        missing_skills=missing_skills or [],
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate
