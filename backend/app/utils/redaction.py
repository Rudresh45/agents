"""PII redaction applied before any text leaves the system for an LLM (R9)."""

from __future__ import annotations

import re

from pydantic import BaseModel

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", re.IGNORECASE)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
DOB_RE = re.compile(
    r"(?im)\b(date\s+of\s+birth|dob|born\s+on)\b\s*[:\-]?\s*[\d./\-]{4,12}"
)
AGE_RE = re.compile(r"(?im)\b(age)\b\s*[:\-]?\s*\d{1,2}\b")
GENDER_RE = re.compile(r"(?im)\b(gender|sex)\b\s*[:\-]?\s*[A-Za-z/ ,.-]{2,30}")
NATIONALITY_RE = re.compile(
    r"(?im)\b(nationality|citizenship|religion|caste)\b\s*[:\-]?\s*[A-Za-z ,/.-]{2,40}"
)
MARITAL_RE = re.compile(
    r"(?im)\b(marital\s+status)\b\s*[:\-]?\s*[A-Za-z ,/.-]{2,30}"
)
PHOTO_RE = re.compile(
    r"(?im)\b(photo|photograph|passport\s+size\s+photo)\b[^\n]{0,40}"
)
ADDRESS_LINE_RE = re.compile(
    r"(?im)^.*\b(address|pin\s*code|postal\s*code|zip\s*code|residence)\b.*$"
)

LABEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", EMAIL_RE),
    ("url", URL_RE),
    ("dob", DOB_RE),
    ("age", AGE_RE),
    ("gender", GENDER_RE),
    ("nationality", NATIONALITY_RE),
    ("marital_status", MARITAL_RE),
    ("photo", PHOTO_RE),
    ("address", ADDRESS_LINE_RE),
]


class RedactionResult(BaseModel):
    text: str
    removed: list[str]


def _redact_phones(text: str) -> tuple[str, bool]:
    removed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal removed
        digits = sum(char.isdigit() for char in match.group(0))
        if digits < 10:
            return match.group(0)
        removed = True
        return "[PHONE]"

    return PHONE_CANDIDATE_RE.sub(_replace, text), removed


def _redact_name(text: str, known_name: str) -> tuple[str, bool]:
    removed = False
    parts = [part for part in re.split(r"\s+", known_name.strip()) if len(part) >= 3]
    for value in [known_name.strip(), *parts]:
        if not value:
            continue
        pattern = re.compile(re.escape(value), re.IGNORECASE)
        text, count = pattern.subn("[NAME]", text)
        removed = removed or count > 0
    return text, removed


def redact_pii(text: str, *, known_name: str | None = None) -> RedactionResult:
    removed: set[str] = set()

    # Contact and label patterns run first so a name replacement cannot corrupt
    # an email address or URL into an unmatched fragment.
    for label, pattern in LABEL_PATTERNS:
        text, count = pattern.subn(f"[{label.upper()}]", text)
        if count:
            removed.add(label)

    text, phone_removed = _redact_phones(text)
    if phone_removed:
        removed.add("phone")

    if known_name:
        text, did_remove = _redact_name(text, known_name)
        if did_remove:
            removed.add("name")

    # Collapse the blank lines left behind by removed address blocks.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return RedactionResult(text=text, removed=sorted(removed))
