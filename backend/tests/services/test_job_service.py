from __future__ import annotations

import pytest
from app.core.constants import JobStatus
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.schemas.domain import Actor, JobInput, JobUpdate
from app.services.job_service import JobService
from sqlalchemy.orm import Session


def _job_input(**overrides) -> JobInput:
    data = {
        "title": "Backend Engineer",
        "required_skills": ["python", "sql"],
        "preferred_skills": ["fastapi"],
        "min_experience_years": 2,
        "location": "Bengaluru",
    }
    data.update(overrides)
    return JobInput(**data)


def test_create_uses_default_min_score(session: Session, actor: Actor):
    job = JobService(session).create(_job_input(), actor=actor)
    assert job.min_score == 60
    assert job.status == JobStatus.OPEN


def test_create_requires_required_skills(session: Session, actor: Actor):
    with pytest.raises(ValidationFailedError):
        JobService(session).create(_job_input(required_skills=[" "]), actor=actor)


def test_email_alias_must_be_unique(session: Session, actor: Actor):
    service = JobService(session)
    service.create(_job_input(email_alias="jobs@acme.com"), actor=actor)
    with pytest.raises(ValidationFailedError):
        service.create(
            _job_input(title="Other", email_alias="JOBS@acme.com"), actor=actor
        )


def test_update_and_close(session: Session, actor: Actor):
    service = JobService(session)
    job = service.create(_job_input(), actor=actor)
    updated = service.update(job.id, JobUpdate(min_score=75), actor=actor)
    assert updated.min_score == 75
    closed = service.close(job.id, actor=actor)
    assert closed.status == JobStatus.CLOSED
    assert closed.closed_at is not None
    with pytest.raises(ValidationFailedError):
        service.update(job.id, JobUpdate(min_score=80), actor=actor)


def test_close_is_idempotent(session: Session, actor: Actor):
    service = JobService(session)
    job = service.create(_job_input(), actor=actor)
    service.close(job.id, actor=actor)
    assert service.close(job.id, actor=actor).status == JobStatus.CLOSED


def test_list_and_open_jobs(session: Session, actor: Actor):
    service = JobService(session)
    first = service.create(_job_input(), actor=actor)
    service.create(_job_input(title="Analyst"), actor=actor)
    service.close(first.id, actor=actor)

    rows, total = service.list(status=JobStatus.OPEN)
    assert total == 1
    assert rows[0].title == "Analyst"
    assert len(service.open_jobs()) == 1


def test_missing_job_raises(session: Session):
    with pytest.raises(NotFoundError):
        JobService(session).get("nope")
