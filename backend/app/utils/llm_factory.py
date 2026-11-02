"""The only entry point to the LLM provider (R5).

Agents call `get_llm().invoke(schema, system=..., user=..., prompt_version=...)`
and always receive a validated Pydantic object.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Protocol, TypeVar

import structlog
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ExternalServiceError,
    LLMNotConfiguredError,
    RateLimitedError,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class StructuredLLM(Protocol):
    def invoke(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        prompt_version: str,
    ) -> T: ...


class _MinInterval:
    """Serialises calls so a free tier is not burst-limited."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.seconds <= 0:
            return
        with self._lock:
            remaining = self._last + self.seconds - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._last = time.monotonic()


def _looks_rate_limited(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("429", "quota", "rate limit", "resource_exhausted", "too many requests")
    )


class GeminiStructuredLLM:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._limiter = _MinInterval(self.settings.llm_min_interval_seconds)
        self._model = None

    def _build_model(self):  # noqa: ANN202
        if not self.settings.gemini_api_key:
            raise LLMNotConfiguredError("GEMINI_API_KEY is empty")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=self.settings.gemini_model,
            google_api_key=self.settings.gemini_api_key,
            temperature=0,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=0,
        )

    def _get_model(self):  # noqa: ANN202
        if self._model is None:
            self._model = self._build_model()
        return self._model

    def invoke(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        prompt_version: str,
    ) -> T:
        from langchain_core.messages import HumanMessage, SystemMessage

        chain = self._get_model().with_structured_output(schema)
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        attempts = self.settings.llm_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            self._limiter.wait()
            started = time.perf_counter()
            try:
                raw = chain.invoke(messages)
                result = raw if isinstance(raw, schema) else schema.model_validate(raw)
                logger.info(
                    "llm_call",
                    prompt_version=prompt_version,
                    model=self.settings.gemini_model,
                    attempt=attempt + 1,
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    outcome="ok",
                )
                return result
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "llm_invalid_output",
                    prompt_version=prompt_version,
                    attempt=attempt + 1,
                    error=str(exc)[:300],
                )
                messages = [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            f"{user}\n\nYour previous reply did not match the required schema. "
                            f"Errors: {str(exc)[:800]}\nReturn only valid JSON that matches it."
                        )
                    ),
                ]
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "llm_call_failed",
                    prompt_version=prompt_version,
                    attempt=attempt + 1,
                    error=str(exc)[:300],
                )
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 8))

        message = str(last_error) if last_error else "unknown error"
        if isinstance(last_error, ValidationError):
            raise ExternalServiceError(
                "The model returned output that did not match the schema",
                code="llm_invalid_output",
                details={"prompt_version": prompt_version},
            ) from last_error
        if _looks_rate_limited(message):
            raise RateLimitedError(details={"prompt_version": prompt_version}) from last_error
        raise ExternalServiceError(
            "The LLM call failed",
            code="llm_failed",
            details={"prompt_version": prompt_version},
        ) from last_error


@lru_cache
def get_llm() -> StructuredLLM:
    return GeminiStructuredLLM()
