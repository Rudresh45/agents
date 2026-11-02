"""Application error hierarchy and FastAPI exception handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for expected, mapped application errors."""

    code = "application_error"
    status_code = 400
    message = "Application error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    message = "Resource not found"


class ValidationFailedError(AppError):
    code = "validation_error"
    status_code = 422
    message = "Validation failed"


class DuplicateCandidateError(AppError):
    code = "duplicate_candidate"
    status_code = 409
    message = "Candidate already exists for this job"


class InvalidTransitionError(AppError):
    code = "invalid_transition"
    status_code = 409
    message = "Status transition is not allowed"

    def __init__(self, old_status: str, new_status: str) -> None:
        super().__init__(
            f"Cannot move candidate from '{old_status}' to '{new_status}'",
            details={"from": old_status, "to": new_status},
        )


class ReasonRequiredError(AppError):
    code = "reason_required"
    status_code = 422
    message = "A written reason is required for this change"


class ApprovalRequiredError(AppError):
    code = "approval_required"
    status_code = 409
    message = "At least one approved draft is required before sending"


class RunAlreadyActiveError(AppError):
    code = "run_already_active"
    status_code = 409
    message = "A workflow run is already active"


class ConflictError(AppError):
    code = "conflict"
    status_code = 409
    message = "The request conflicts with the current state"


class StepLockedError(AppError):
    code = "step_locked"
    status_code = 409
    message = "This workflow step is already running"


class ExternalServiceError(AppError):
    code = "external_service_error"
    status_code = 502
    message = "An external service failed"


class LLMNotConfiguredError(AppError):
    code = "llm_not_configured"
    status_code = 503
    message = "The LLM provider is not configured"


class RateLimitedError(AppError):
    code = "rate_limited"
    status_code = 429
    message = "Rate limit reached, retry later"


class AuthError(AppError):
    code = "unauthorized"
    status_code = 401
    message = "Authentication required"


class ForbiddenError(AppError):
    code = "forbidden"
    status_code = 403
    message = "You do not have permission to perform this action"


class AuditImmutableError(AppError):
    code = "audit_immutable"
    status_code = 409
    message = "Audit log entries are append-only"


def _envelope(request: Request, code: str, message: str, details: Any = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", extra={"code": exc.code}, exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(err.get("loc", [])), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope(request, "validation_error", "Request validation failed", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content=_envelope(request, "internal_error", "An unexpected error occurred"),
        )
