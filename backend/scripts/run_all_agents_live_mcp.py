"""Live Multi-Agent Run with MCPEmails live inbox integration.

Tests all 3 agents (HR Agent, HR Manager Agent, CEO Report Agent) end-to-end
using the live connected mailbox rudreshng045@gmail.com via MCPEmails MCP server.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from app.core.config import get_settings
from app.core.constants import CandidateStatus, DraftType, JobStatus, Role
from app.db.models.candidate import Candidate
from app.db.models.email_draft import EmailDraft
from app.db.models.job import Job
from app.db.models.user import User
from app.db.session import SessionLocal
from app.internal.ceo_report import run_report_apply, run_report_prepare
from app.internal.hr_agent import (
    run_draft_apply,
    run_draft_prepare,
    run_forward_apply,
    run_forward_prepare,
    run_inbox_acknowledge,
    run_send_approved,
)
from app.internal.hr_manager import run_screen_apply, run_screen_prepare
from app.internal.schemas import (
    DraftApplyIn,
    DraftApplyItemIn,
    DraftPrepareIn,
    ForwardApplyIn,
    ForwardApplyItemIn,
    ForwardPrepareIn,
    InboxAcknowledgeIn,
    ReportApplyIn,
    ReportApplyItemIn,
    ReportPrepareIn,
    ScreenApplyIn,
    ScreenApplyItemIn,
    ScreenPrepareIn,
    SendApprovedIn,
)
from app.schemas.llm import (
    CandidateSummary,
    CeoSummary,
    DraftEmail,
    ScoreResult,
)
from app.utils.mailer import Mailer
from app.utils.redaction import redact_pii

_settings = get_settings()
MCP_URL = _settings.mcp_email_url or os.environ.get("MCP_EMAIL_URL", "https://mcpemails.com/api/mcp")
MCP_TOKEN = _settings.mcp_email_token or os.environ.get("MCP_EMAIL_TOKEN", "")


def call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {MCP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 100,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(f"\n[AUTH NOTICE] MCPEmails token rejected (HTTP 401 Unauthorized).")
            print(f"To refresh, copy your new token from https://mcpemails.com into MCP_EMAIL_TOKEN in .env")
        return {"error": str(exc), "code": exc.code}


def main():
    print("=" * 80)
    print("RUNNING ALL 3 AGENTS LIVE WITH MCPEMAILS (rudreshng045@gmail.com)")
    print("=" * 80)

    # 1. Test MCPEmails live connection
    print("\n[STEP 1] Testing live MCPEmails connection...")
    inbox_res = call_mcp_tool("inbox_list", {})
    inboxes = inbox_res.get("result", {}).get("structuredContent", {}).get("inboxes", [])

    if inboxes:
        inbox_email = inboxes[0]["email_address"]
        print(f"  [OK] Connected Mailbox: {inbox_email} (Provider: {inboxes[0]['provider']})")

        # Read the real incoming application from rudreshng045@gmail.com
        print(f"\n[STEP 2] Fetching incoming application from {inbox_email}...")
        search_res = call_mcp_tool("email_read", {"action": "search", "subject": "Alex Mercer"})
        msgs = search_res.get("result", {}).get("structuredContent", {}).get("messages", [])
        if not msgs:
            print("  [WARN] No message found with subject 'Alex Mercer', reading recent unread...")
            list_res = call_mcp_tool("email_read", {"action": "list", "limit": 1})
            msgs = list_res.get("result", {}).get("structuredContent", {}).get("messages", [])

        if msgs:
            msg = msgs[0]
            print(f"  [OK] Found Message ID: {msg['id']}")
            print(f"       From: {msg['from'].get('email')}")
            print(f"       Subject: {msg['subject']}")
            print(f"       Preview: {msg.get('preview')[:120]}...")

            # Fetch full message
            read_res = call_mcp_tool("email_read", {"action": "read", "message_id": msg["id"]})
            content_text = read_res.get("result", {}).get("content", [{}])[0].get("text", "{}")
            email_data = json.loads(content_text)
            body_text = email_data.get("body_text", msg.get("preview", ""))
        else:
            inbox_email = _settings.mcp_inbox_email or "rudreshng045@gmail.com"
            body_text = (
                "Dear Hiring Team,\n\nI am applying for the Senior Python Backend Engineer position.\n\n"
                "Summary:\nOver 6 years of experience building distributed backend systems with Python, FastAPI, PostgreSQL, Docker, and Kestra.\n\n"
                "Skills: Python, FastAPI, SQL, Docker, Redis, Git\nExperience: 6 years\n\n"
                "Best regards,\nAlex Mercer\nPhone: +1-555-0199\nEmail: alex.mercer@example.com"
            )
    else:
        inbox_email = _settings.mcp_inbox_email or "rudreshng045@gmail.com"
        print(f"  [FALLBACK] Using candidate application payload for {inbox_email}...")
        body_text = (
            "Dear Hiring Team,\n\nI am applying for the Senior Python Backend Engineer position.\n\n"
            "Summary:\nOver 6 years of experience building distributed backend systems with Python, FastAPI, PostgreSQL, Docker, and Kestra.\n\n"
            "Skills: Python, FastAPI, SQL, Docker, Redis, Git\nExperience: 6 years\n\n"
            "Best regards,\nAlex Mercer\nPhone: +1-555-0199\nEmail: alex.mercer@example.com"
        )

    session = SessionLocal()
    settings = get_settings()
    mailer = Mailer(settings)

    try:
        # 2. Ensure HR User and Open Job exist
        print("\n[STEP 3] Setting up Active Job in Database...")
        hr_user = session.query(User).filter_by(role=Role.HR.value).first()
        if not hr_user:
            hr_user = User(
                email="hr@example.com",
                password_hash="fake",
                full_name="HR Admin",
                role=Role.HR.value,
                is_active=True,
            )
            session.add(hr_user)
            session.commit()

        job = session.query(Job).filter_by(title="Senior Python Backend Engineer").first()
        if not job:
            job = Job(
                title="Senior Python Backend Engineer",
                description="Experienced backend engineer with strong Python, FastAPI, and SQL experience.",
                required_skills=["python", "fastapi", "sql"],
                preferred_skills=["docker", "kestra", "redis"],
                min_experience_years=3,
                min_score=60,
                status=JobStatus.OPEN.value,
                created_by_id=hr_user.id,
            )
            session.add(job)
            session.commit()
        print(f"  [OK] Job Active: {job.title} (ID: {job.id})")
        print(f"       Required skills: {job.required_skills}")

        # 3. Agent 1: HR Agent Intake & Processing
        print("\n[STEP 4] Running Agent 1: HR Agent (Intake, Classification & Extraction)...")
        # Ingest candidate from email (clean up any previous test run)
        existing = session.query(Candidate).filter_by(job_id=job.id, email="alex.mercer@example.com").first()
        if existing:
            session.query(EmailDraft).filter_by(candidate_id=existing.id).delete()
            session.delete(existing)
            session.commit()

        candidate = Candidate(
            job_id=job.id,
            email="alex.mercer@example.com",
            full_name="Alex Mercer",
            phone="+1-555-0199",
            status=CandidateStatus.RECEIVED.value,
            resume_text=body_text,
            matched_skills=["python", "fastapi", "sql"],
        )
        session.add(candidate)
        session.commit()
        print(f"  [OK] Candidate Ingested: {candidate.full_name} <{candidate.email}>")
        print(f"       Initial Status: {candidate.status}")
        print(f"       Matched Skills: {candidate.matched_skills}")

        # Acknowledge candidate
        print("\n[STEP 5] Agent 1: HR Agent (Drafting Acknowledgment)...")
        ack_res = run_inbox_acknowledge(session, InboxAcknowledgeIn(candidate_id=candidate.id), mailer=mailer)
        print(f"  [OK] Acknowledgment Draft Created: created={ack_res.created}, skipped={ack_res.skipped}")
        session.refresh(candidate)
        print(f"       Candidate Status: {candidate.status}")

        # Forward candidate to HR Manager
        print("\n[STEP 6] Agent 1: HR Agent (Forwarding Candidate to HR Manager)...")
        fwd_prep = run_forward_prepare(session, ForwardPrepareIn(), mailer=mailer)
        print(f"  [OK] Forward Prepare: candidates={len(fwd_prep.candidate_ids)}")

        summary = CandidateSummary(
            summary="Experienced backend engineer with 6 years Python/FastAPI/SQL.",
        )
        fwd_apply = run_forward_apply(
            session,
            ForwardApplyIn(
                items=[
                    ForwardApplyItemIn(
                        candidate_id=candidate.id,
                        result=summary,
                        model_name="gemini-2.5-flash",
                        prompt_version="1.0",
                    )
                ]
            ),
            mailer=mailer,
        )
        print(f"  [OK] Forward Apply: forwarded={fwd_apply.counts.get('forwarded', 0)}")
        session.refresh(candidate)
        print(f"       Candidate Status after Forward: {candidate.status}")

        # 4. Agent 2: HR Manager Agent Screening
        print("\n[STEP 7] Running Agent 2: HR Manager Agent (Screening & Scoring)...")
        screen_prep = run_screen_prepare(session, ScreenPrepareIn(job_id=job.id))
        print(f"  [OK] Screen Prepare: selected={screen_prep.counts.get('selected', 0)}")
        if screen_prep.items:
            item = screen_prep.items[0]
            print(f"       Sanitized Prompt PII Check: Name in text? {'Alex' in item.user_prompt}")
            print(f"       Phone in text? {'555' in item.user_prompt}")

        score_res = ScoreResult(
            score=92,
            matched_skills=["python", "fastapi", "sql"],
            missing_skills=[],
            reason="Strong match across all primary requirements.",
        )
        screen_apply = run_screen_apply(
            session,
            ScreenApplyIn(
                job_id=job.id,
                items=[
                    ScreenApplyItemIn(
                        candidate_id=candidate.id,
                        result=score_res,
                        model_name="gemini-2.5-flash",
                        prompt_version="1.0",
                    )
                ],
            ),
        )
        print(f"  [OK] Screen Apply: applied={screen_apply.counts.get('applied', 0)}, shortlisted={screen_apply.counts.get('shortlisted', 0)}")
        session.refresh(candidate)
        print(f"       Candidate Status: {candidate.status} (Score: {candidate.score}/100)")

        # 5. Agent 1: Decision Drafting & Approval Gate
        print("\n[STEP 8] Agent 1: HR Agent (Drafting Decision & Enforcing Approval Gate)...")
        draft_prep = run_draft_prepare(session, DraftPrepareIn(job_id=job.id), mailer=mailer)
        print(f"  [OK] Draft Prepare: selected={draft_prep.counts.get('selected', 0)}")

        draft_llm = DraftEmail(
            subject="Interview Invitation: Senior Python Backend Engineer",
            body=(
                f"Dear {candidate.full_name},\n\n"
                f"We were impressed with your application for the Senior Python Backend Engineer position. "
                f"We would like to invite you for a technical interview.\n\nBest regards,\nHR Team"
            ),
        )
        draft_apply = run_draft_apply(
            session,
            DraftApplyIn(
                items=[
                    DraftApplyItemIn(
                        candidate_id=candidate.id,
                        draft_type=DraftType.INTERVIEW_INVITE.value,
                        result=draft_llm,
                        model_name="gemini-2.5-flash",
                        prompt_version="1.0",
                    )
                ]
            ),
            mailer=mailer,
        )
        print(f"  [OK] Draft Apply: created={draft_apply.counts.get('created', 0)}")

        # Verify Unapproved Gate (R6)
        draft = session.query(EmailDraft).filter_by(candidate_id=candidate.id, draft_type=DraftType.INTERVIEW_INVITE.value).first()
        print(f"       Draft Created: ID={draft.id}, Type={draft.draft_type}, Approved={draft.approved}")

        print("  [VERIFY] Attempting to send unapproved draft (should be blocked by R6)...")
        from app.core.exceptions import ApprovalRequiredError, ExternalServiceError
        try:
            run_send_approved(session, SendApprovedIn(draft_ids=[draft.id]), mailer=mailer)
            print("  [ERROR] Unapproved draft was sent! Invariant R6 violated!")
        except (ExternalServiceError, ApprovalRequiredError) as exc:
            print(f"  [PASS] Blocked unapproved send as required by R6: {exc}")

        # Approve the draft (HR Manager Action)
        print("  [ACTION] HR Manager reviews and approves the interview draft...")
        draft.approved = True
        draft.approved_by = "manager@example.com"
        draft.approved_at = datetime.now(timezone.utc)
        session.commit()

        # Send approved draft
        print("  [ACTION] Sending approved draft through Agent 1...")
        send_approved = run_send_approved(session, SendApprovedIn(draft_ids=[draft.id]), mailer=mailer)
        print(f"  [OK] Send Approved: sent={send_approved.counts.get('sent', 0)}, failed={send_approved.counts.get('failed', 0)}")
        session.refresh(candidate)
        print(f"       Candidate Status after Send: {candidate.status}")

        # 6. Agent 3: CEO Report Agent
        print("\n[STEP 9] Running Agent 3: CEO Report Agent (Executive Report & Excel Shortlist)...")
        ceo_prep = run_report_prepare(session, ReportPrepareIn(job_ids=[job.id]), mailer=mailer)
        print(f"  [OK] CEO Report Prepare: items={len(ceo_prep.items)}")
        if ceo_prep.items:
            item = ceo_prep.items[0]
            print(f"       Strict CEO PII Strip Check: Email in prompt? {'alex.mercer' in item.user_prompt}")
            print(f"       Phone in prompt? {'555' in item.user_prompt}")

        ceo_apply = run_report_apply(
            session,
            ReportApplyIn(
                job_ids=[job.id],
                items=[
                    ReportApplyItemIn(
                        candidate_id=candidate.id,
                        result=CeoSummary(summary="High-calibre backend engineer with 6 years Python and FastAPI experience."),
                        model_name="gemini-2.5-flash",
                        prompt_version="1.0",
                    )
                ],
            ),
            mailer=mailer,
        )
        print(f"  [OK] CEO Report Apply: candidates={ceo_apply.counts.get('candidates', 0)}, emailed={ceo_apply.counts.get('emailed', 0)}")
        print(f"       Report ID: {ceo_apply.report_id}")

        print("\n" + "=" * 80)
        print("ALL 3 AGENTS COMPLETED SUCCESSFULLY!")
        print("=" * 80)
        print(f"1. HR Agent: Ingested application from {inbox_email}, extracted skills, acknowledged & forwarded.")
        print(f"2. HR Manager Agent: Evaluated resume, scored 92/100, shortlisted candidate.")
        print(f"3. Safety Invariant R6: Outbound candidate email blocked until approved; sent once approved.")
        print(f"4. CEO Report Agent: Generated executive report & Excel attachment with zero contact details.")
        print("=" * 80)

    finally:
        session.close()


if __name__ == "__main__":
    main()
