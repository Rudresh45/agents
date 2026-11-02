from __future__ import annotations

import pytest
from app.core.config import Settings
from app.core.exceptions import (
    ExternalServiceError,
    LLMNotConfiguredError,
    RateLimitedError,
)
from app.schemas.llm import ScoreResult
from app.utils.llm_factory import GeminiStructuredLLM


class _Chain:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.messages: list = []

    def invoke(self, messages):
        self.messages.append(messages)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Model:
    def __init__(self, chain: _Chain) -> None:
        self.chain = chain

    def with_structured_output(self, _schema):
        return self.chain


def _llm(outcomes: list, **overrides) -> tuple[GeminiStructuredLLM, _Chain]:
    settings = Settings(
        gemini_api_key="fake-key",
        llm_min_interval_seconds=0,
        llm_max_retries=overrides.pop("retries", 2),
        **overrides,
    )
    llm = GeminiStructuredLLM(settings)
    chain = _Chain(outcomes)
    llm._get_model = lambda: _Model(chain)  # type: ignore[method-assign]
    return llm, chain


VALID = {
    "score": 80,
    "matched_skills": ["python"],
    "missing_skills": ["docker"],
    "reason": "Strong Python background.",
}


def test_returns_validated_schema():
    llm, _ = _llm([VALID])
    result = llm.invoke(
        ScoreResult, system="s", user="u", prompt_version="screening.v1"
    )
    assert isinstance(result, ScoreResult)
    assert result.score == 80


def test_repairs_invalid_output_once():
    llm, chain = _llm([{"score": 150}, VALID])
    result = llm.invoke(
        ScoreResult, system="s", user="u", prompt_version="screening.v1"
    )
    assert result.score == 80
    assert len(chain.messages) == 2
    repair_text = chain.messages[1][1].content
    assert "did not match the required schema" in repair_text


def test_missing_api_key_raises_not_configured():
    llm = GeminiStructuredLLM(Settings(gemini_api_key="", llm_min_interval_seconds=0))
    with pytest.raises(LLMNotConfiguredError):
        llm.invoke(ScoreResult, system="s", user="u", prompt_version="screening.v1")


def test_rate_limit_is_mapped():
    llm, _ = _llm([Exception("429 quota exceeded")], retries=0)
    with pytest.raises(RateLimitedError):
        llm.invoke(ScoreResult, system="s", user="u", prompt_version="screening.v1")


def test_transport_failure_is_mapped_after_retries():
    llm, chain = _llm([Exception("boom"), Exception("boom"), Exception("boom")], retries=2)
    with pytest.raises(ExternalServiceError):
        llm.invoke(ScoreResult, system="s", user="u", prompt_version="screening.v1")
    assert len(chain.messages) == 3


def test_recovers_after_one_transport_failure():
    llm, chain = _llm([Exception("temporary"), VALID], retries=2)
    result = llm.invoke(
        ScoreResult, system="s", user="u", prompt_version="screening.v1"
    )
    assert result.score == 80
    assert len(chain.messages) == 2
