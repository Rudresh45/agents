from __future__ import annotations

import httpx
import pytest
from app.core.config import Settings
from app.core.exceptions import ExternalServiceError
from app.utils.kestra_client import KestraClient


def _settings(**overrides) -> Settings:
    values = {
        "kestra_url": "http://kestra.test",
        "kestra_api_token": "kestra-secret",
        "kestra_timeout_seconds": 5,
        "kestra_max_retries": 1,
        "kestra_retry_base_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def test_start_screening_sends_job_id_as_multipart() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == (
            "/api/v1/executions/hr.recruitment/hr_screening_and_decisions"
        )
        assert request.headers["authorization"] == "Bearer kestra-secret"
        body = request.read()
        assert b'name="job_id"' in body
        assert b"job-1" in body
        assert b'name="run_id"' in body
        assert b"run-1" in body
        return httpx.Response(
            200,
            json={"id": "exec-1", "state": {"current": "CREATED"}},
        )

    with KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        execution = client.start_screening(
            "job-1", run_id="run-1", correlation_id="run-1"
        )

    assert execution.id == "exec-1"
    assert execution.state == "CREATED"
    assert seen[0].url.params["labels"] == "system.correlationId:run-1"


def test_basic_auth_is_used_for_kestra_oss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Basic YWRtaW5AZXhhbXBsZS5jb206c2VjcmV0"
        return httpx.Response(
            200,
            json={"id": "exec-1", "state": {"current": "RUNNING"}},
        )

    settings = _settings(
        kestra_api_token="",
        kestra_api_username="admin@example.com",
        kestra_api_password="secret",
    )
    with KestraClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert client.get_state("exec-1") == "RUNNING"


def test_get_state_reads_kestra_state_and_retries_transient_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.path == "/api/v1/executions/exec-1"
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": "exec-1", "state": {"current": "PAUSED"}})

    with KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        assert client.get_state("exec-1") == "PAUSED"

    assert calls == 2


def test_start_is_not_retried_to_avoid_duplicate_executions() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with (
        KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ExternalServiceError),
    ):
        client.start_screening("job-1")

    assert calls == 1


def test_resume_sends_approval_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/executions/exec-1/resume"
        body = request.read()
        assert b'name="approved"' in body
        assert b"true" in body
        assert b'name="approved_by"' in body
        assert b"hr@example.com" in body
        return httpx.Response(204)

    with KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        client.resume_execution("exec-1", approved=True, approved_by="hr@example.com")

    assert len(seen) == 1


def test_resume_treats_not_paused_as_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    with KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        client.resume_execution("exec-1", approved=True, approved_by="hr@example.com")


def test_http_error_becomes_external_service_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    with (
        KestraClient(_settings(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ExternalServiceError),
    ):
        client.get_state("missing")
