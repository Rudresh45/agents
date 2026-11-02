from __future__ import annotations

from datetime import UTC, datetime

from app.core.constants import CandidateStatus, DraftType
from app.core.security import hash_password
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from app.db.models.user import User
from app.schemas.domain import Actor, ApplicationInput
from app.services.candidate_service import CandidateService
from app.services.draft_service import DraftService
from app.services.reply_service import ReplyService
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate

ACTOR = Actor.agent("test")


def job_payload(**overrides) -> dict:
    payload = {
        "title": "Data Engineer",
        "description": "Build pipelines.",
        "required_skills": ["python", "sql"],
        "preferred_skills": ["airflow"],
        "min_experience_years": 2,
        "location": "Remote",
        "min_score": 55,
    }
    payload.update(overrides)
    return payload


def test_routes_require_a_token(client: TestClient) -> None:
    for path in ("/api/v1/jobs", "/api/v1/candidates", "/api/v1/audit"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"


def test_hr_cannot_create_a_job(client: TestClient, auth_headers: dict) -> None:
    response = client.post("/api/v1/jobs", json=job_payload(), headers=auth_headers)
    assert response.status_code == 403


def test_manager_creates_updates_and_closes_a_job(
    client: TestClient, manager_headers: dict
) -> None:
    created = client.post(
        "/api/v1/jobs", json=job_payload(), headers=manager_headers
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["title"] == "Data Engineer"
    assert job["status"] == "open"

    updated = client.patch(
        f"/api/v1/jobs/{job['id']}",
        json={"min_score": 70, "location": "Bengaluru"},
        headers=manager_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["min_score"] == 70

    closed = client.post(
        f"/api/v1/jobs/{job['id']}/close", headers=manager_headers
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_ceo_can_read_jobs_but_not_candidates(
    client: TestClient, ceo_headers: dict
) -> None:
    assert client.get("/api/v1/jobs", headers=ceo_headers).status_code == 200
    assert client.get("/api/v1/candidates", headers=ceo_headers).status_code == 403


def test_candidate_list_filters_and_pagination(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    make_candidate(session, job_id=job.id, full_name="Asha Rao", email="asha@example.com")
    make_candidate(
        session,
        job_id=job.id,
        full_name="Ravi Kumar",
        email="ravi@example.com",
        status=CandidateStatus.SHORTLISTED,
        score=81,
    )

    response = client.get(
        "/api/v1/candidates?status=shortlisted&sort=-score&page=1&page_size=1",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["pages"] == 1
    assert body["items"][0]["full_name"] == "Ravi Kumar"

    search = client.get(
        "/api/v1/candidates?q=asha", headers=auth_headers
    )
    assert search.status_code == 200
    assert search.json()["total"] == 1


def test_candidate_detail_contact_update_and_decision(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=75,
        summary="Strong backend engineer.",
    )
    DraftService(session).create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Body",
        actor=ACTOR,
        run_id=None,
    )

    detail = client.get(f"/api/v1/candidates/{candidate.id}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["candidate"]["id"] == candidate.id
    assert body["job"]["id"] == job.id
    assert len(body["drafts"]) == 1
    assert body["consent_flagged"] is True

    patched = client.patch(
        f"/api/v1/candidates/{candidate.id}",
        json={"full_name": "Asha R Rao", "phone": "+91 90000 00000"},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["full_name"] == "Asha R Rao"

    decided = client.post(
        f"/api/v1/candidates/{candidate.id}/decision",
        json={"decision": "not_shortlisted", "reason": "HR override"},
        headers=auth_headers,
    )
    assert decided.status_code == 200
    assert decided.json()["candidate"]["status"] == CandidateStatus.NOT_SHORTLISTED
    assert decided.json()["drafts_superseded"] is True


def test_assign_job_moves_needs_review_to_received(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=None,
        status=CandidateStatus.NEEDS_REVIEW,
        email="review@example.com",
    )
    response = client.post(
        f"/api/v1/candidates/{candidate.id}/assign-job",
        json={"job_id": job.id},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == CandidateStatus.RECEIVED
    assert response.json()["job_id"] == job.id


def test_withdraw_requires_a_reason(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
    )
    missing = client.post(
        f"/api/v1/candidates/{candidate.id}/withdraw",
        json={},
        headers=auth_headers,
    )
    assert missing.status_code == 422

    ok = client.post(
        f"/api/v1/candidates/{candidate.id}/withdraw",
        json={"reason": "Candidate accepted another offer"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == CandidateStatus.WITHDRAWN


def test_delete_candidate_creates_a_data_request(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(session, job_id=job.id)
    response = client.delete(
        f"/api/v1/candidates/{candidate.id}", headers=auth_headers
    )
    assert response.status_code == 202
    assert response.json()["status"] == "open"

    requests = client.get("/api/v1/privacy/requests", headers=auth_headers)
    assert requests.status_code == 200
    assert len(requests.json()) == 1
    assert requests.json()[0]["request_type"] == "deletion"


def test_draft_edit_approve_and_bulk_approve(
    client: TestClient, session: Session, auth_headers: dict, job, run
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
    )
    draft = DraftService(session).create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Body",
        actor=ACTOR,
        run_id=run.id,
    )

    listed = client.get(
        f"/api/v1/drafts?candidate_id={candidate.id}", headers=auth_headers
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    edited = client.patch(
        f"/api/v1/drafts/{draft.id}",
        json={"subject": "Interview invitation"},
        headers=auth_headers,
    )
    assert edited.status_code == 200
    assert edited.json()["subject"] == "Interview invitation"

    approved = client.post(
        f"/api/v1/drafts/{draft.id}/approve", headers=auth_headers
    )
    assert approved.status_code == 200
    assert approved.json()["approved"] is True

    edited_after_approval = client.patch(
        f"/api/v1/drafts/{draft.id}",
        json={"body": "A changed body needs fresh approval."},
        headers=auth_headers,
    )
    assert edited_after_approval.status_code == 200
    assert edited_after_approval.json()["approved"] is False
    assert edited_after_approval.json()["approved_at"] is None

    reapproved = client.post(
        f"/api/v1/drafts/{draft.id}/approve", headers=auth_headers
    )
    assert reapproved.status_code == 200
    assert reapproved.json()["approved"] is True

    revoked = client.post(
        f"/api/v1/drafts/{draft.id}/revoke", headers=auth_headers
    )
    assert revoked.status_code == 200
    assert revoked.json()["approved"] is False

    bulk = client.post(
        "/api/v1/drafts/bulk-approve",
        json={"draft_ids": [draft.id, "missing-id"]},
        headers=auth_headers,
    )
    assert bulk.status_code == 200
    results = {item["draft_id"]: item for item in bulk.json()}
    assert results[draft.id]["approved"] is True
    assert results["missing-id"]["approved"] is False


def test_sent_draft_cannot_be_edited(
    client: TestClient, session: Session, auth_headers: dict, job, run
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
    )
    draft = DraftService(session).create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview",
        body="Body",
        actor=ACTOR,
        run_id=run.id,
    )
    row = session.get(EmailDraft, draft.id)
    row.sent_at = datetime.now(UTC)
    session.commit()

    response = client.patch(
        f"/api/v1/drafts/{draft.id}",
        json={"subject": "Changed"},
        headers=auth_headers,
    )
    assert response.status_code == 409


def test_reply_handling_and_withdraw(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.SHORTLISTED,
        score=80,
    )
    reply = ReplyService(session).record(
        candidate_id=candidate.id,
        category="withdrawn",
        actor=ACTOR,
        body_excerpt="Please withdraw my application.",
    )

    listed = client.get("/api/v1/replies", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    handled = client.post(
        f"/api/v1/replies/{reply.id}/handle",
        json={"action": "mark_handled"},
        headers=auth_headers,
    )
    assert handled.status_code == 200
    assert handled.json()["handled"] is True


def test_audit_list_and_csv_export(
    client: TestClient, session: Session, auth_headers: dict, manager_headers: dict, job
) -> None:
    CandidateService(session).create_from_application(
        ApplicationInput(
            full_name="Asha Rao",
            email="asha@example.com",
            job_id=job.id,
        ),
        actor=ACTOR,
    )

    assert client.get("/api/v1/audit", headers=manager_headers).status_code == 403

    listed = client.get("/api/v1/audit?entity_type=candidate", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] >= 1

    exported = client.get("/api/v1/audit/export.csv", headers=auth_headers)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "candidate.created" in exported.text


def test_privacy_access_export_and_deletion(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(session, job_id=job.id)
    session.add(
        EmailLog(
            direction="inbound",
            message_id="<privacy-test@example.com>",
            candidate_id=candidate.id,
            from_address="asha@example.com",
            to_address="hr@acme-hr.com",
            subject="Application",
            status="received",
        )
    )
    session.commit()

    created = client.post(
        "/api/v1/privacy/requests",
        params={"candidate_id": candidate.id, "request_type": "access"},
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]

    completed = client.post(
        f"/api/v1/privacy/requests/{request_id}/complete", headers=auth_headers
    )
    assert completed.status_code == 200
    assert completed.json()["completed"] is True
    assert completed.json()["export"]["candidate"]["id"] == candidate.id

    deleted = client.delete(
        f"/api/v1/candidates/{candidate.id}", headers=auth_headers
    )
    deletion_id = deleted.json()["request_id"]
    result = client.post(
        f"/api/v1/privacy/requests/{deletion_id}/complete", headers=auth_headers
    )
    assert result.status_code == 200
    assert result.json()["deleted"] is True
    assert client.get(
        f"/api/v1/candidates/{candidate.id}", headers=auth_headers
    ).status_code == 404

    log = session.query(EmailLog).filter_by(message_id="<privacy-test@example.com>").one()
    assert log.candidate_id is None
    assert log.from_address.startswith("sha256:")
    assert log.to_address.startswith("sha256:")


def test_ceo_report_generation_listing_and_download(
    client: TestClient, session: Session, auth_headers: dict, ceo_headers: dict, job
) -> None:
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.ACCEPTED,
        score=88,
        summary="Strong backend engineer.",
    )

    generated = client.post(
        "/api/v1/reports/ceo",
        json={"send": False},
        headers=auth_headers,
    )
    assert generated.status_code == 202, generated.text
    report_id = generated.json()["report_id"]
    assert report_id

    listed = client.get("/api/v1/reports/ceo", headers=ceo_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    detail = client.get(f"/api/v1/reports/ceo/{report_id}", headers=ceo_headers)
    assert detail.status_code == 200
    assert detail.json()["items"][0]["candidate_name"] == "Asha Rao"

    download = client.get(
        f"/api/v1/reports/ceo/{report_id}/download", headers=ceo_headers
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )

    pdf = client.get(
        f"/api/v1/reports/ceo/{report_id}/download?format=pdf",
        headers=ceo_headers,
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


def test_logs_are_hr_only(
    client: TestClient, auth_headers: dict, manager_headers: dict, ceo_headers: dict
) -> None:
    assert client.get("/api/v1/logs", headers=auth_headers).status_code == 200
    assert client.get("/api/v1/logs", headers=manager_headers).status_code == 403
    assert client.get("/api/v1/logs", headers=ceo_headers).status_code == 403


def test_reports_role_matrix(
    client: TestClient,
    auth_headers: dict,
    manager_headers: dict,
    ceo_headers: dict,
) -> None:
    assert client.get("/api/v1/reports/ceo", headers=manager_headers).status_code == 403
    assert client.get("/api/v1/reports/ceo", headers=ceo_headers).status_code == 200
    assert client.get("/api/v1/reports/ceo", headers=auth_headers).status_code == 200

    for headers in (manager_headers, ceo_headers):
        response = client.post(
            "/api/v1/reports/ceo", json={"send": False}, headers=headers
        )
        assert response.status_code == 403


def test_request_id_is_echoed_and_in_error_envelopes(
    client: TestClient, auth_headers: dict
) -> None:
    ok = client.get(
        "/api/v1/jobs", headers={**auth_headers, "X-Request-ID": "trace-123"}
    )
    assert ok.headers["X-Request-ID"] == "trace-123"

    generated = client.get("/api/v1/jobs")
    assert generated.status_code == 401
    request_id = generated.headers["X-Request-ID"]
    assert request_id
    assert generated.json()["error"]["request_id"] == request_id


def test_missing_and_invalid_identifiers(
    client: TestClient, auth_headers: dict
) -> None:
    assert client.get(
        "/api/v1/jobs/does-not-exist", headers=auth_headers
    ).status_code == 404
    assert client.get(
        "/api/v1/candidates/does-not-exist", headers=auth_headers
    ).status_code == 404
    assert client.get(
        "/api/v1/candidates?page=0", headers=auth_headers
    ).status_code == 422
    assert client.get(
        "/api/v1/candidates?page_size=500", headers=auth_headers
    ).status_code == 422


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"healthy", "degraded"}


def test_openapi_schema_covers_the_main_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/v1/workflow/run",
        "/api/v1/workflow/status/{run_id}",
        "/api/v1/workflow/approve",
        "/api/v1/jobs",
        "/api/v1/candidates",
        "/api/v1/drafts",
        "/api/v1/replies",
        "/api/v1/reports/ceo",
        "/api/v1/audit",
        "/api/v1/audit/export.csv",
        "/api/v1/privacy/sweep",
        "/api/v1/logs",
        "/health",
    ):
        assert path in paths, path
    assert client.get("/docs").status_code == 200


def test_unseeded_user_is_rejected(client: TestClient, session: Session) -> None:
    user = User(
        email="other@example.com",
        password_hash=hash_password("secret"),
        role="hr",
        full_name="Other",
    )
    session.add(user)
    session.commit()
    token_response = client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": "wrong"},
    )
    assert token_response.status_code == 401
    assert token_response.json()["error"]["code"] == "unauthorized"


def test_job_list_rejects_unknown_sort(client: TestClient, auth_headers: dict) -> None:
    response = client.get(
        "/api/v1/candidates?sort=password", headers=auth_headers
    )
    assert response.status_code == 422


def test_create_job_validates_required_skills(
    client: TestClient, manager_headers: dict
) -> None:
    response = client.post(
        "/api/v1/jobs",
        json={"title": "No skills"},
        headers=manager_headers,
    )
    assert response.status_code == 422


def test_candidate_reprocess_requires_a_resume(
    client: TestClient, session: Session, auth_headers: dict, job
) -> None:
    candidate = make_candidate(session, job_id=job.id)
    response = client.post(
        f"/api/v1/candidates/{candidate.id}/reprocess", headers=auth_headers
    )
    assert response.status_code == 422
