"""Small, typed HTTP client for the Kestra execution API.

Kestra owns orchestration; this client only starts, reads and resumes the
screening flow. It deliberately logs execution ids and HTTP states, never
request bodies (resume text and prompts must not reach application logs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger(__name__)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
START_STATUS_CODES = frozenset({200, 201, 202})
READ_STATUS_CODES = frozenset({200})
RESUME_STATUS_CODES = frozenset({200, 202, 204, 409})
KILL_STATUS_CODES = frozenset({200, 202, 204, 404, 409})


@dataclass(frozen=True)
class KestraExecution:
    """The subset of an execution response the backend needs."""

    id: str
    state: str
    namespace: str | None = None
    flow_id: str | None = None


class KestraClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.kestra_url.rstrip("/")
        headers = {"Accept": "application/json"}
        auth: httpx.Auth | None = None
        if self.settings.kestra_api_username and self.settings.kestra_api_password:
            # Kestra OSS 1.3 requires basic auth. Do not send a Bearer token at
            # the same time; HTTP only allows one Authorization header.
            auth = httpx.BasicAuth(
                self.settings.kestra_api_username,
                self.settings.kestra_api_password,
            )
        elif self.settings.kestra_api_token:
            headers["Authorization"] = f"Bearer {self.settings.kestra_api_token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.settings.kestra_timeout_seconds,
            headers=headers,
            auth=auth,
            transport=transport,
        )

    def __enter__(self) -> KestraClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def start_screening(
        self,
        job_id: str,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> KestraExecution:
        """Start one screening execution for a job.

        Starts are not retried automatically: a timeout after Kestra accepted
        the request would create a duplicate execution. Callers may retry the
        user request safely once the failed run is terminal.

        ``run_id`` lets every prepare/apply call target the same backend run.
        ``correlation_id`` is only a Kestra label for tracing.
        """
        params = None
        if correlation_id:
            params = {"labels": f"system.correlationId:{correlation_id}"}
        files: dict[str, tuple[None, str]] = {"job_id": (None, job_id)}
        if run_id:
            files["run_id"] = (None, run_id)
        response = self._request(
            "POST",
            f"/api/v1/executions/{self.settings.kestra_namespace}/"
            f"{self.settings.kestra_flow_id}",
            expected=START_STATUS_CODES,
            retryable=False,
            params=params,
            files=files,
        )
        return self._execution(response)

    def get_execution(self, execution_id: str) -> KestraExecution:
        response = self._request(
            "GET", f"/api/v1/executions/{execution_id}", expected=READ_STATUS_CODES
        )
        return self._execution(response)

    def get_state(self, execution_id: str) -> str:
        return self.get_execution(execution_id).state

    def resume_execution(
        self, execution_id: str, *, approved: bool, approved_by: str
    ) -> None:
        # Kestra resumes a Pause task through multipart form fields. A 409
        # means the execution is no longer paused, which is the desired
        # end-state for an idempotent retry.
        self._request(
            "POST",
            f"/api/v1/executions/{execution_id}/resume",
            expected=RESUME_STATUS_CODES,
            files={
                "approved": (None, "true" if approved else "false"),
                "approved_by": (None, approved_by),
            },
        )

    def kill_execution(self, execution_id: str) -> None:
        self._request(
            "DELETE",
            f"/api/v1/executions/{execution_id}/kill",
            expected=KILL_STATUS_CODES,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: frozenset[int],
        retryable: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        attempts = self.settings.kestra_max_retries + 1 if retryable else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._retry_log(method, path, attempt, "transport_error")
                continue

            if response.status_code in expected:
                return response

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                self._retry_log(method, path, attempt, f"http_{response.status_code}")
                continue

            logger.error(
                "kestra_request_failed",
                method=method,
                path=path,
                status_code=response.status_code,
            )
            raise ExternalServiceError(
                f"Kestra {method} {path} failed with HTTP {response.status_code}"
            )

        logger.error(
            "kestra_request_failed",
            method=method,
            path=path,
            error=type(last_error).__name__ if last_error else "retry_exhausted",
        )
        raise ExternalServiceError("Kestra request failed") from last_error

    def _retry_log(self, method: str, path: str, attempt: int, reason: str) -> None:
        delay = self.settings.kestra_retry_base_seconds * (2 ** (attempt - 1))
        logger.warning(
            "kestra_request_retry",
            method=method,
            path=path,
            attempt=attempt,
            reason=reason,
        )
        if delay:
            time.sleep(delay)

    @staticmethod
    def _execution(response: httpx.Response) -> KestraExecution:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("Kestra returned an invalid JSON response") from exc

        execution_id = payload.get("id")
        state = (payload.get("state") or {}).get("current")
        if not execution_id or not state:
            raise ExternalServiceError("Kestra response did not include an execution id/state")

        return KestraExecution(
            id=str(execution_id),
            state=str(state),
            namespace=payload.get("namespace"),
            flow_id=payload.get("flowId"),
        )
