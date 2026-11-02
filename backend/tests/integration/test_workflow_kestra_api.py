from __future__ import annotations

from app.api.deps import get_kestra_client
from app.core.config import get_settings
from app.core.constants import CandidateStatus, DraftType
from app.schemas.domain import Actor
from app.services.draft_service import DraftService
from app.services.workflow_service import WorkflowService
from app.utils.kestra_client import KestraExecution
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.agents.conftest import make_candidate


class FakeKestraClient:
    def __init__(self, state: str = "CREATED") -> None:
        self.state = state
        self.execution_id = "kestra-exec-1"
        self.started: list[tuple[str, str | None, str | None]] = []
        self.resumed: list[tuple[str, bool, str]] = []
        self.killed: list[str] = []

    def start_screening(
        self,
        job_id: str,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> KestraExecution:
        self.execution_id = f"kestra-exec-{len(self.started) + 1}"
        self.started.append((job_id, run_id, correlation_id))
        return KestraExecution(id=self.execution_id, state=self.state)

    def get_execution(self, execution_id: str) -> KestraExecution:
        assert execution_id == self.execution_id
        return KestraExecution(id=execution_id, state=self.state)

    def resume_execution(
        self, execution_id: str, *, approved: bool, approved_by: str
    ) -> None:
        self.resumed.append((execution_id, approved, approved_by))
        self.state = "RUNNING"

    def kill_execution(self, execution_id: str) -> None:
        self.killed.append(execution_id)
        self.state = "KILLED"


def _enable_kestra(client: TestClient, settings, fake: FakeKestraClient) -> None:
    client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"use_kestra_llm": True}
    )
    client.app.dependency_overrides[get_kestra_client] = lambda: fake


def test_kestra_run_starts_flow_and_stores_execution_id(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient()
    _enable_kestra(client, settings, fake)

    response = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kestra_execution_id"] == "kestra-exec-1"
    assert fake.started == [(job.id, body["run_id"], body["run_id"])]
    stored = WorkflowService(session).get(body["run_id"])
    assert stored.kestra_execution_id == "kestra-exec-1"


def test_kestra_requires_job_id(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient()
    _enable_kestra(client, settings, fake)

    response = client.post("/api/v1/workflow/run", json={}, headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert fake.started == []


def test_kestra_status_pause_approve_and_finish(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient(state="CREATED")
    _enable_kestra(client, settings, fake)

    started = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    )
    run_id = started.json()["run_id"]

    fake.state = "PAUSED"
    paused = client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "waiting_approval"
    assert paused.json()["kestra_state"] == "PAUSED"

    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.FORWARDED
    )
    draft = DraftService(session).create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview invitation",
        body="Please join us.",
        actor=Actor.system("hr_agent"),
        run_id=run_id,
    )

    approved = client.post(
        "/api/v1/workflow/approve",
        json={"run_id": run_id, "draft_ids": [draft.id]},
        headers=auth_headers,
    )
    assert approved.status_code == 202, approved.text
    assert fake.resumed == [("kestra-exec-1", True, "hr@acme-hr.com")]
    assert WorkflowService(session).get(run_id).status == "running"

    fake.state = "SUCCESS"
    finished = client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)
    assert finished.json()["status"] == "finished"
    assert finished.json()["kestra_state"] == "SUCCESS"


def test_kestra_approve_is_idempotent_after_resume(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient(state="PAUSED")
    _enable_kestra(client, settings, fake)

    run_id = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    ).json()["run_id"]
    client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)

    candidate = make_candidate(
        session, job_id=job.id, status=CandidateStatus.FORWARDED
    )
    draft = DraftService(session).create_draft(
        candidate_id=candidate.id,
        draft_type=DraftType.INTERVIEW_INVITE,
        subject="Interview invitation",
        body="Please join us.",
        actor=Actor.system("hr_agent"),
        run_id=run_id,
    )

    payload = {"run_id": run_id, "draft_ids": [draft.id]}
    first = client.post("/api/v1/workflow/approve", json=payload, headers=auth_headers)
    second = client.post("/api/v1/workflow/approve", json=payload, headers=auth_headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(fake.resumed) == 1


def test_kestra_approve_without_an_approved_draft_sends_nothing(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient(state="PAUSED")
    _enable_kestra(client, settings, fake)

    run_id = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    ).json()["run_id"]
    client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)

    response = client.post(
        "/api/v1/workflow/approve",
        json={"run_id": run_id},
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_required"
    assert fake.resumed == []


def test_kestra_failed_state_frees_the_active_slot(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient(state="FAILED")
    _enable_kestra(client, settings, fake)

    run_id = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    ).json()["run_id"]

    failed = client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)
    assert failed.json()["status"] == "failed"
    assert WorkflowService(session).active_run() is None


def test_kestra_force_cancels_the_old_execution(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient()
    _enable_kestra(client, settings, fake)

    first = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    )
    second = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id, "force": True},
        headers=auth_headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202, second.text
    assert fake.killed == ["kestra-exec-1"]
    assert second.json()["kestra_execution_id"] == "kestra-exec-2"


def test_kestra_cancel_kills_the_execution(
    client: TestClient, session: Session, settings, auth_headers: dict, job
):
    fake = FakeKestraClient(state="PAUSED")
    _enable_kestra(client, settings, fake)

    run_id = client.post(
        "/api/v1/workflow/run",
        json={"job_id": job.id},
        headers=auth_headers,
    ).json()["run_id"]
    client.get(f"/api/v1/workflow/status/{run_id}", headers=auth_headers)

    cancelled = client.post(
        f"/api/v1/workflow/{run_id}/cancel", headers=auth_headers
    )

    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert fake.killed == ["kestra-exec-1"]
