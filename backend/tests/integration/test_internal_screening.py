from __future__ import annotations

from app.core.constants import CandidateStatus
from app.db.models.audit_log import AuditLog
from app.services.email_log_service import EmailLogService
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate

INTERNAL_TOKEN = "test-internal-service-token"
HEADERS = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


def _prepare(client: TestClient, job_id: str, **extra):
    return client.post(
        "/internal/hr-manager/screen/prepare",
        json={"job_id": job_id, **extra},
        headers=HEADERS,
    )


def _apply(client: TestClient, items: list[dict], **extra):
    return client.post(
        "/internal/hr-manager/screen/apply",
        json={"items": items, **extra},
        headers=HEADERS,
    )


def _score(score: int = 80) -> dict:
    return {
        "score": score,
        "matched_skills": ["python", "sql"],
        "missing_skills": ["fastapi"],
        "reason": "Relevant backend experience.",
    }


def _item(candidate_id: str, prompt_version: str, score: int = 80) -> dict:
    return {
        "candidate_id": candidate_id,
        "result": _score(score),
        "model_name": "gemini-test",
        "prompt_version": prompt_version,
    }


def test_prepare_returns_only_forwarded_sanitised_input(
    client: TestClient, session: Session, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        full_name="Asha Rao",
        email="asha@example.com",
        resume_text=(
            "Asha Rao\n"
            "asha@example.com\n"
            "+91 98765 43210\n"
            "Six years of Python and SQL experience."
        ),
    )
    make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.RECEIVED,
        email="other@example.com",
        full_name="Other Person",
    )

    response = _prepare(client, job.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_work"] is True
    assert body["counts"]["selected"] == 1
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["candidate_id"] == candidate.id
    assert item["job_id"] == job.id
    assert item["prompt_version"] == "screening.v1"
    assert item["response_schema"]["properties"]["score"]["maximum"] == 100
    assert "SECURITY RULE" in item["system_prompt"]

    prompt = f"{item['system_prompt']}\n{item['user_prompt']}"
    assert "Asha" not in prompt
    assert "asha@example.com" not in prompt
    assert "98765" not in prompt
    assert "[NAME]" in item["user_prompt"]
    assert "[EMAIL]" in item["user_prompt"]

    # PREPARE is read-only for scoreable candidates, so a retry sees the same work.
    again = _prepare(client, job.id)
    assert again.status_code == 200
    assert [row["candidate_id"] for row in again.json()["items"]] == [candidate.id]


def test_prepare_moves_no_resume_candidate_to_review_once(
    client: TestClient, session: Session, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="",
    )

    first = _prepare(client, job.id)

    assert first.status_code == 200, first.text
    assert first.json()["counts"]["skipped"] == 1
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.NEEDS_REVIEW

    second = _prepare(client, job.id)

    assert second.status_code == 200
    assert second.json()["counts"]["selected"] == 0
    assert second.json()["counts"]["skipped"] == 0
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.NEEDS_REVIEW


def test_apply_scores_shortlists_and_sends_once(
    client: TestClient, session: Session, job, run
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="Six years of Python and SQL experience.",
    )
    prepared = _prepare(client, job.id).json()
    item = prepared["items"][0]

    payload = {
        "items": [_item(item["candidate_id"], item["prompt_version"], score=80)],
        "job_id": job.id,
        "run_id": run.id,
        "idempotency_key": "test-execution-1",
    }
    first = client.post(
        "/internal/hr-manager/screen/apply", json=payload, headers=HEADERS
    )

    assert first.status_code == 200, first.text
    assert first.json()["counts"]["applied"] == 1
    assert first.json()["counts"]["shortlisted"] == 1
    assert first.json()["shortlist"]["counts"]["emailed"] == 1

    session.refresh(candidate)
    assert candidate.status == CandidateStatus.SHORTLISTED
    assert candidate.score == 80

    _logs, total = EmailLogService(session).list(direction="outbound")
    assert total == 1

    second = client.post(
        "/internal/hr-manager/screen/apply", json=payload, headers=HEADERS
    )

    assert second.status_code == 200, second.text
    assert second.json()["counts"]["applied"] == 0
    assert second.json()["counts"]["skipped"] == 1
    assert second.json()["shortlist"]["counts"]["skipped"] == 1

    _logs, total = EmailLogService(session).list(direction="outbound")
    assert total == 1


def test_apply_finalize_sends_shortlist_once_after_item_apply(
    client: TestClient, session: Session, job, run
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="Six years of Python and SQL experience.",
    )
    prepared = _prepare(client, job.id).json()
    item = prepared["items"][0]

    first = _apply(
        client,
        [_item(item["candidate_id"], item["prompt_version"])],
        job_id=job.id,
        run_id=run.id,
        idempotency_key="test-execution-2",
        finalize=False,
    )

    assert first.status_code == 200, first.text
    assert first.json()["counts"]["applied"] == 1
    assert first.json()["shortlist"] == {}
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.SHORTLISTED
    _logs, total = EmailLogService(session).list(direction="outbound")
    assert total == 0

    finalize = _apply(
        client,
        [],
        job_id=job.id,
        run_id=run.id,
        idempotency_key="test-execution-2",
    )

    assert finalize.status_code == 200, finalize.text
    assert finalize.json()["shortlist"]["counts"]["emailed"] == 1
    _logs, total = EmailLogService(session).list(direction="outbound")
    assert total == 1

    repeated = _apply(
        client,
        [],
        job_id=job.id,
        run_id=run.id,
        idempotency_key="test-execution-2",
    )

    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["shortlist"]["counts"]["skipped"] == 1
    _logs, total = EmailLogService(session).list(direction="outbound")
    assert total == 1


def test_apply_rejects_score_above_100(
    client: TestClient, session: Session, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="Six years of Python and SQL experience.",
    )
    prepared = _prepare(client, job.id).json()
    item = prepared["items"][0]

    response = _apply(
        client,
        [_item(item["candidate_id"], item["prompt_version"], score=101)],
        job_id=job.id,
    )

    assert response.status_code == 422
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FORWARDED
    assert candidate.score is None


def test_apply_rejects_malformed_result_json(
    client: TestClient, session: Session, job
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        resume_text="Six years of Python and SQL experience.",
    )
    prepared = _prepare(client, job.id).json()
    item = _item(prepared["items"][0]["candidate_id"], prepared["items"][0]["prompt_version"])
    item["result"] = "{not-json"

    response = _apply(client, [item], job_id=job.id)

    assert response.status_code == 422
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FORWARDED
    assert candidate.score is None


def test_apply_reuses_existing_score_to_finish_transition(
    client: TestClient, session: Session, job, run
) -> None:
    candidate = make_candidate(
        session,
        job_id=job.id,
        status=CandidateStatus.FORWARDED,
        score=80,
        reason="Relevant backend experience.",
        matched_skills=["python", "sql"],
        missing_skills=["fastapi"],
        resume_text="Six years of Python and SQL experience.",
    )
    candidate.scored_model = "gemini-test"
    candidate.prompt_version = "screening.v1"
    session.commit()

    response = _apply(
        client,
        [_item(candidate.id, "screening.v1", score=80)],
        job_id=job.id,
        run_id=run.id,
        idempotency_key="test-execution-3",
    )

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["applied"] == 1
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.SHORTLISTED

    scored_audits = session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "candidate.scored"
        )
    )
    assert scored_audits == 0


def test_screening_routes_require_service_token(client: TestClient, job) -> None:
    prepare = client.post(
        "/internal/hr-manager/screen/prepare", json={"job_id": job.id}
    )
    assert prepare.status_code == 401

    apply = client.post(
        "/internal/hr-manager/screen/apply",
        json={"items": [], "finalize": True},
    )
    assert apply.status_code == 401


def test_screening_routes_are_hidden_from_public_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert "/internal/hr-manager/screen/prepare" not in spec["paths"]
    assert "/internal/hr-manager/screen/apply" not in spec["paths"]
