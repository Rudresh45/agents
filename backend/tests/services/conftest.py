from __future__ import annotations

import pytest
from app.core.security import hash_password
from app.db.models.candidate import Candidate
from app.db.models.job import Job
from app.db.models.user import User
from app.schemas.domain import Actor
from sqlalchemy.orm import Session


@pytest.fixture()
def hr_user(session: Session) -> User:
    user = User(
        email="hr@example.com",
        password_hash=hash_password("secret"),
        role="hr",
        full_name="HR Admin",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def actor(hr_user: User) -> Actor:
    return Actor.from_user(hr_user)


@pytest.fixture()
def job(session: Session, hr_user: User) -> Job:
    row = Job(
        title="Backend Engineer",
        required_skills=["python", "sql"],
        preferred_skills=["fastapi"],
        min_experience_years=2,
        min_score=60,
        created_by_id=hr_user.id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture()
def candidate(session: Session, job: Job) -> Candidate:
    row = Candidate(
        full_name="A Candidate",
        email="candidate@example.com",
        job_id=job.id,
        resume_text="Python and SQL experience.",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
