from __future__ import annotations

from typing import Any, TypeVar

from app.core.exceptions import ExternalServiceError
from pydantic import BaseModel

T = TypeVar("T")


class FakeStructuredLLM:
    """Deterministic stand-in for the Gemini client.

    responses maps either a schema class name or a prompt_version to a value,
    a callable (user_text -> value), or a list of values consumed in order.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        fail_times: int = 0,
        fail_with: Exception | None = None,
    ) -> None:
        self.responses = responses or {}
        self.fail_times = fail_times
        self.fail_with = fail_with or ExternalServiceError("llm down", code="llm_failed")
        self.calls: list[dict[str, str]] = []

    def invoke(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        prompt_version: str,
    ) -> T:
        self.calls.append(
            {
                "schema": schema.__name__,
                "prompt_version": prompt_version,
                "system": system,
                "user": user,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with

        response = self.responses.get(schema.__name__)
        if response is None:
            response = self.responses.get(prompt_version)
        if response is None:
            raise AssertionError(f"No fake response for {schema.__name__} / {prompt_version}")

        if isinstance(response, list):
            response = response.pop(0)
        if callable(response):
            response = response(user)
        if isinstance(response, BaseModel):
            return response  # type: ignore[return-value]
        return schema.model_validate(response)
