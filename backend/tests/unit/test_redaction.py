from __future__ import annotations

from app.utils.redaction import redact_pii

RESUME = """Priya Sharma
priya.sharma@example.com | +91 98765 43210
Bengaluru, India

Gender: Female
Date of Birth: 12/03/1995
Nationality: Indian
Marital status: Single
Address: 12 MG Road, Bengaluru 560001

Skills: Python, FastAPI, PostgreSQL, Docker
Experience: 5 years building backend services.
"""


def test_redaction_removes_direct_identifiers():
    result = redact_pii(RESUME, known_name="Priya Sharma")
    text = result.text

    assert "Priya" not in text
    assert "priya.sharma@example.com" not in text
    assert "98765" not in text
    assert "12/03/1995" not in text
    assert "Female" not in text
    assert "Indian" not in text
    assert "12 MG Road" not in text
    assert "Single" not in text


def test_redaction_keeps_job_relevant_content():
    result = redact_pii(RESUME, known_name="Priya Sharma")
    assert "Python" in result.text
    assert "FastAPI" in result.text
    assert "5 years" in result.text


def test_redaction_reports_categories_not_values():
    result = redact_pii(RESUME, known_name="Priya Sharma")
    assert "name" in result.removed
    assert "email" in result.removed
    assert "phone" in result.removed
    assert "gender" in result.removed
    assert "dob" in result.removed
    assert "nationality" in result.removed
    assert "address" in result.removed
    assert "priya" not in " ".join(result.removed).lower()


def test_redaction_without_known_name():
    result = redact_pii("Contact me at someone@example.com or 9988776655")
    assert "someone@example.com" not in result.text
    assert "9988776655" not in result.text


def test_redaction_leaves_short_numbers_alone():
    text = "Worked 2019-2023. Scored 95 percent. Team of 12."
    result = redact_pii(text)
    assert "2019-2023" in result.text
    assert "95" in result.text
    assert "12" in result.text


def test_redaction_does_not_treat_github_url_as_skill():
    result = redact_pii("GitHub: https://github.com/priya\nPython developer")
    assert "github.com/priya" not in result.text
    assert "Python developer" in result.text
