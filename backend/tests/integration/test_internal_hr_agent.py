from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.core.constants import CandidateStatus, DraftType
from app.db.models.audit_log import AuditLog
from app.db.models.candidate import Candidate
from app.db.models.ceo_report import CeoReport, CeoReportItem
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from app.internal import ceo_report as ceo_report_adapter
from app.internal import hr_agent as hr_agent_adapter
from app.internal.deps import get_imap_client, get_mailer
from app.schemas.domain import Actor
from app.services.draft_service import DraftService
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.agents.conftest import (
    make_candidate,
    make_email,
    make_pdf,
    resume_attachment,
)
from tests.fakes import FakeImapClient, FakeMailer

INTERNAL_TOKEN = "test-internal-service-token"
HEADERS = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}

APPLICATION_BODY = (
    "Priya Sharma\n"
    "priya@example.com\n"
    "+91 98765 43210\n"
    "Backend engineer with six years of Python and SQL experience."
)


@pytest.fixture()
def internal(client: TestClient, settings, monkeypatch):
    """Point the adapters at the test settings and fake mail/IMAP."""
    mailer = FakeMailer(settings)
    imap = FakeImapClient(attachment_bytes=make_pdf(*APPLICATION_BODY.splitlines()))
    monkeypatch.setattr(hr_agent_adapter, "get_settings", lambda: settings)
    monkeypatch.setattr(ceo_report_adapter, "get_settings", lambda: settings)
    client.app.dependency_overrides[get_mailer] = lambda: mailer
    client.app.dependency_overrides[get_imap_client] = lambda: imap
    return SimpleNamespace(mailer=mailer, imap=imap, settings=settings)


def _prepare_inbox(client: TestClient, **extra) -> dict:
    response = client.post(
        "/internal/hr-agent/inbox/prepare",
        json={"limit": 10, **extra},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _apply_application(client: TestClient, item: dict, job_id: str, **extra):
    return client.post(
        "/internal/hr-agent/inbox/apply",
        json={
            "email_key": item["email_key"],
            "classification": {
                "is_application": True,
                "confidence": 0.95,
                "reason": "resume attached",
            },
            "classification_model": "gemini-test",
            "classification_prompt_version": item["classification"]["prompt_version"],
            "job_match": {
                "job_id": job_id,
                "confidence": 0.9,
                "rationale": "backend skills",
            },
            "job_match_model": "gemini-test",
            "job_match_prompt_version": item["job_match"]["prompt_version"],
            **extra,
        },
        headers=HEADERS,
    )


def test_new_internal_routes_require_service_token(client: TestClient) -> None:
    paths = [
        "/internal/hr-agent/inbox/prepare",
        "/internal/hr-agent/inbox/apply",
        "/internal/hr-agent/inbox/reply",
        "/internal/hr-agent/inbox/acknowledge",
        "/internal/hr-agent/forward/prepare",
        "/internal/hr-agent/forward/apply",
        "/internal/hr-agent/draft/prepare",
        "/internal/hr-agent/draft/apply",
        "/internal/hr-agent/send/approved",
        "/internal/ceo-report/prepare",
        "/internal/ceo-report/apply",
        "/internal/alerts",
    ]
    for path in paths:
        response = client.post(path, json={})
        assert response.status_code == 401, (path, response.text)


def test_inbox_prepare_returns_redacted_ai_input_only(
    client: TestClient, session: Session, job, internal
) -> None:
    internal.imap.emails = [
        make_email(
            sender="Priya Sharma <priya@example.com>",
            subject="Application for Backend Engineer",
            body=APPLICATION_BODY,
            attachments=[resume_attachment()],
        )
    ]

    body = _prepare_inbox(client)

    assert body["has_work"] is True
    assert body["counts"]["selected"] == 1
    assert len(body["applications"]) == 1
    assert len(body["ai_applications"]) == 1
    assert body["alias_applications"] == []
    item = body["ai_applications"][0]
    assert item["job_match"] is not None
    assert item["classification"]["system_prompt"]
    assert item["classification"]["prompt_version"] == "classify_email.v1"
    rendered = json.dumps(body)
    assert "priya@example.com" not in rendered
    assert "+91 98765 43210" not in rendered
    assert "Priya Sharma" not in rendered
    # The opaque key is the only identifier sent to Kestra.
    assert item["email_key"] and "@" not in item["email_key"]
    staging = internal.settings.upload_dir / ".staging" / "inbox"
    assert list(staging.glob("*.json"))


def test_inbox_prepare_splits_alias_matched_items(
    client: TestClient, session: Session, job, internal
) -> None:
    job.email_alias = "jobs@example.com"
    session.commit()
    internal.imap.emails = [
        make_email(to="jobs@example.com", subject="Application", body=APPLICATION_BODY)
    ]

    body = _prepare_inbox(client)

    assert len(body["alias_applications"]) == 1
    assert body["ai_applications"] == []
    assert body["alias_applications"][0]["job_id"] == job.id
    assert body["alias_applications"][0]["job_match"] is None


def test_inbox_apply_is_idempotent_and_creates_acknowledgement(
    client: TestClient, session: Session, job, run, internal
) -> None:
    internal.imap.emails = [
        make_email(
            sender="Priya Sharma <priya@example.com>",
            subject="Application for Backend Engineer",
            body=APPLICATION_BODY,
            attachments=[resume_attachment()],
        )
    ]
    item = _prepare_inbox(client, run_id=run.id)["ai_applications"][0]

    first = _apply_application(client, item, job.id, run_id=run.id)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["counts"]["created"] == 1
    assert body["candidate_id"]
    assert body["draft_type"] == DraftType.ACKNOWLEDGEMENT

    candidate = session.get(Candidate, body["candidate_id"])
    assert candidate is not None
    assert candidate.source_email_id == item["email_key"]
    assert candidate.resume_text
    assert session.scalar(
        select(func.count()).select_from(Candidate)
    ) == 1

    second = _apply_application(client, item, job.id, run_id=run.id)
    assert second.status_code == 200, second.text
    assert second.json()["counts"]["skipped"] == 1
    assert second.json()["candidate_id"] == body["candidate_id"]
    assert session.scalar(
        select(func.count()).select_from(Candidate)
    ) == 1

    logs = session.scalars(
        select(EmailLog).where(EmailLog.direction == "inbound")
    ).all()
    assert len(logs) == 1
    assert logs[0].candidate_id == body["candidate_id"]


def test_acknowledge_creates_one_draft(
    client: TestClient, session: Session, job, run, internal
) -> None:
    candidate = make_candidate(session, job_id=job.id, status=CandidateStatus.RECEIVED)
    payload = {
        "candidate_id": candidate.id,
        "draft_type": DraftType.ACKNOWLEDGEMENT,
        "run_id": run.id,
    }

    first = client.post(
        "/internal/hr-agent/inbox/acknowledge", json=payload, headers=HEADERS
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    assert first.json()["draft_id"]

    second = client.post(
        "/internal/hr-agent/inbox/acknowledge", json=payload, headers=HEADERS
    )
    assert second.status_code == 200, second.text
    assert second.json()["skipped"] is True
    assert session.scalar(select(func.count()).select_from(EmailDraft)) == 1

    skipped = client.post(
        "/internal/hr-agent/inbox/acknowledge",
        json={**payload, "draft_type": None},
        headers=HEADERS,
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["skipped"] is True


def test_forward_prepare_apply_and_digest_are_idempotent(
    client: TestClient, session: Session, job, run, internal
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.RECEIVED,
        full_name="Asha Rao",
        email="asha@example.com",
        resume_text="Asha Rao\nasha@example.com\nPython and SQL for six years.",
    )

    prepare = client.post(
        "/internal/hr-agent/forward/prepare",
        json={"limit": 10, "run_id": run.id},
        headers=HEADERS,
    )
    assert prepare.status_code == 200, prepare.text
    body = prepare.json()
    assert body["has_work"] is True
    item = body["items"][0]
    assert item["candidate_id"] == candidate.id
    assert "asha@example.com" not in item["user_prompt"]

    summary = {
        "candidate_id": candidate.id,
        "result": {"summary": "Strong Python and SQL background."},
        "model_name": "gemini-test",
        "prompt_version": item["prompt_version"],
    }
    applied = client.post(
        "/internal/hr-agent/forward/apply",
        json={
            "items": [summary],
            "candidate_ids": [candidate.id],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": False,
        },
        headers=HEADERS,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["counts"]["applied"] == 1

    final = client.post(
        "/internal/hr-agent/forward/apply",
        json={
            "items": [],
            "candidate_ids": body["candidate_ids"],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": True,
        },
        headers=HEADERS,
    )
    assert final.status_code == 200, final.text
    assert final.json()["counts"]["digest_sent"] == 1
    assert len(internal.mailer.outbox) == 1
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FORWARDED
    assert candidate.summary == "Strong Python and SQL background."

    summary_audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "candidate.summary_updated")
    )
    assert summary_audit is not None
    assert summary_audit.model_name == "gemini-test"
    assert summary_audit.prompt_version == item["prompt_version"]

    repeat = client.post(
        "/internal/hr-agent/forward/apply",
        json={
            "items": [],
            "candidate_ids": body["candidate_ids"],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": True,
        },
        headers=HEADERS,
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["counts"]["skipped"] >= 1
    assert len(internal.mailer.outbox) == 1


def test_draft_prepare_and_apply_create_unapproved_drafts(
    client: TestClient, session: Session, job, run, internal
) -> None:
    shortlisted = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        email="short@example.com",
        summary="Strong backend engineer.",
        score=88,
    )
    rejected = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.NOT_SHORTLISTED,
        email="reject@example.com",
        summary="Weak fit for this role.",
        score=20,
    )

    prepare = client.post(
        "/internal/hr-agent/draft/prepare",
        json={"job_id": job.id, "limit": 10, "run_id": run.id},
        headers=HEADERS,
    )
    assert prepare.status_code == 200, prepare.text
    items = prepare.json()["items"]
    assert len(items) == 2
    by_candidate = {item["candidate_id"]: item for item in items}
    assert by_candidate[shortlisted.id]["draft_type"] == DraftType.INTERVIEW_INVITE
    assert by_candidate[rejected.id]["draft_type"] == DraftType.REJECTION

    item = by_candidate[shortlisted.id]
    applied = client.post(
        "/internal/hr-agent/draft/apply",
        json={
            "items": [
                {
                    "candidate_id": item["candidate_id"],
                    "draft_type": item["draft_type"],
                    "result": {
                        "subject": "Interview invitation",
                        "body": "Dear Candidate, please join us.",
                    },
                    "model_name": "gemini-test",
                    "prompt_version": item["prompt_version"],
                }
            ],
            "run_id": run.id,
        },
        headers=HEADERS,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["counts"]["drafts"] == 1
    draft = session.scalar(select(EmailDraft))
    assert draft is not None
    assert draft.approved is False
    assert draft.run_id == run.id
    assert draft.model_name == "gemini-test"

    repeat = client.post(
        "/internal/hr-agent/draft/apply",
        json={
            "items": [
                {
                    "candidate_id": item["candidate_id"],
                    "draft_type": item["draft_type"],
                    "result": {
                        "subject": "Interview invitation",
                        "body": "Dear Candidate, please join us.",
                    },
                    "model_name": "gemini-test",
                    "prompt_version": item["prompt_version"],
                }
            ],
            "run_id": run.id,
        },
        headers=HEADERS,
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["counts"]["skipped"] == 1
    assert session.scalar(select(func.count()).select_from(EmailDraft)) == 1


def test_send_approved_rejects_unapproved_and_sends_once(
    client: TestClient, session: Session, job, run, internal
) -> None:
    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.SHORTLISTED, score=88
    )
    drafts = DraftService(session)
    draft = drafts.create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview invitation",
        body="Dear Candidate, please join us.",
        actor=Actor.agent("hr_agent"),
        run_id=run.id,
    )

    blocked = client.post(
        "/internal/hr-agent/send/approved",
        json={"run_id": run.id, "draft_ids": [draft.id]},
        headers=HEADERS,
    )
    assert blocked.status_code == 409, blocked.text
    assert internal.mailer.outbox == []

    drafts.approve(draft.id, actor=Actor.system())
    sent = client.post(
        "/internal/hr-agent/send/approved",
        json={"run_id": run.id, "draft_ids": [draft.id]},
        headers=HEADERS,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["counts"]["sent"] == 1
    assert len(internal.mailer.outbox) == 1
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.ACCEPTED

    repeat = client.post(
        "/internal/hr-agent/send/approved",
        json={"run_id": run.id, "draft_ids": [draft.id]},
        headers=HEADERS,
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["counts"]["skipped"] == 1
    assert len(internal.mailer.outbox) == 1


def test_ceo_report_prepare_apply_and_one_report_per_run(
    client: TestClient, session: Session, job, run, internal
) -> None:
    accepted = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.ACCEPTED,
        full_name="Asha Rao",
        email="asha@example.com",
        score=88,
        summary="Strong backend engineer with Python and SQL.",
        matched_skills=["python", "sql"],
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.REJECTED,
        email="ravi@example.com",
        full_name="Ravi Kumar",
        score=20,
        summary="Not enough relevant experience.",
    )

    prepare = client.post(
        "/internal/ceo-report/prepare",
        json={"job_ids": [job.id], "run_id": run.id},
        headers=HEADERS,
    )
    assert prepare.status_code == 200, prepare.text
    body = prepare.json()
    assert body["has_work"] is True
    item = body["items"][0]
    assert item["candidate_id"] == accepted.id
    assert "asha@example.com" not in item["user_prompt"]

    applied = client.post(
        "/internal/ceo-report/apply",
        json={
            "items": [
                {
                    "candidate_id": item["candidate_id"],
                    "result": {"summary": "Excellent backend hire."},
                    "model_name": "gemini-test",
                    "prompt_version": item["prompt_version"],
                }
            ],
            "job_ids": [job.id],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": False,
            "send": False,
        },
        headers=HEADERS,
    )
    assert applied.status_code == 200, applied.text
    report_id = applied.json()["report_id"]
    assert report_id
    report = session.get(CeoReport, report_id)
    assert report is not None
    assert report.candidate_count == 1
    assert session.scalar(select(func.count()).select_from(CeoReportItem)) == 1

    final = client.post(
        "/internal/ceo-report/apply",
        json={
            "items": [],
            "job_ids": [job.id],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": True,
            "send": True,
        },
        headers=HEADERS,
    )
    assert final.status_code == 200, final.text
    assert final.json()["counts"]["emailed"] == 1
    assert len(internal.mailer.outbox) == 1
    assert internal.mailer.outbox[0][0].to == ["ceo@example.com"]

    repeat = client.post(
        "/internal/ceo-report/apply",
        json={
            "items": [],
            "job_ids": [job.id],
            "run_id": run.id,
            "idempotency_key": "exec-1",
            "finalize": True,
            "send": True,
        },
        headers=HEADERS,
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["counts"]["skipped"] >= 1
    assert len(internal.mailer.outbox) == 1
    assert session.scalar(select(func.count()).select_from(CeoReport)) == 1


def test_malformed_llm_json_is_rejected(
    client: TestClient, session: Session, job, run, internal
) -> None:
    response = client.post(
        "/internal/hr-agent/inbox/apply",
        json={
            "email_key": "abc",
            "classification": "not-json",
            "classification_model": "gemini-test",
            "classification_prompt_version": "classify_email.v1",
            "run_id": run.id,
        },
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"

    invalid = client.post(
        "/internal/hr-agent/inbox/apply",
        json={
            "email_key": "abc",
            "classification": {
                "is_application": True,
                "confidence": 1.5,
                "reason": "bad confidence",
            },
            "classification_model": "gemini-test",
            "classification_prompt_version": "classify_email.v1",
            "run_id": run.id,
        },
        headers=HEADERS,
    )
    assert invalid.status_code == 422


def test_alert_endpoint_accepts_kestra_failures(
    client: TestClient, monkeypatch
) -> None:
    response = client.post(
        "/internal/alerts",
        json={
            "kind": "kestra_flow_failed",
            "subject": "Flow failed",
            "detail": "Execution exec-1 failed.",
            "dedupe_key": "kestra:exec-1",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert "sent" in response.json()
