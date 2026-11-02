from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent / ".tmp"

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32chars")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LOG_DIR", str(TEST_DIR / "logs"))
os.environ.setdefault("UPLOAD_DIR", str(TEST_DIR / "uploads"))
os.environ.setdefault("EMAIL_DRY_RUN", "true")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-service-token")

from app.api.deps import get_db  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.init_db import seed_users  # noqa: E402
from app.db.models.job import Job  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.db.models.workflow_run import WorkflowRun  # noqa: E402
from app.db.session import build_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas.domain import Actor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

TEST_ACCOUNTS = [
    {
        "email": "hr@acme-hr.com",
        "password": "hr-password-123",
        "role": "hr",
        "full_name": "HR Admin",
    },
    {
        "email": "manager@acme-hr.com",
        "password": "manager-password-123",
        "role": "hr_manager",
        "full_name": "HR Manager",
    },
    {
        "email": "ceo@acme-hr.com",
        "password": "ceo-password-123",
        "role": "ceo",
        "full_name": "CEO",
    },
]


@pytest.fixture()
def db_engine():
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def session(db_engine) -> Session:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        upload_dir=tmp_path / "uploads",
        log_dir=tmp_path / "logs",
        checkpoint_db=tmp_path / "checkpoints.sqlite",
        resume_min_chars=10,
        email_dry_run=True,
        smtp_from="hr@example.com",
        hr_admin_email="hr@example.com",
        hr_manager_notify_email="manager@example.com",
        ceo_report_email="ceo@example.com",
        gemini_api_key="fake-key",
        gemini_model="gemini-test",
        llm_min_interval_seconds=0,
        llm_max_retries=0,
        internal_service_token="test-internal-service-token",
    )


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
        description="Build Python services.",
        required_skills=["python", "sql"],
        preferred_skills=["fastapi"],
        min_experience_years=2,
        min_score=60,
        location="Bengaluru",
        created_by_id=hr_user.id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture()
def run(session: Session) -> WorkflowRun:
    row = WorkflowRun(status="running", dry_run=True)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture()
def seeded(session: Session) -> Session:
    seed_users(session, TEST_ACCOUNTS)
    return session


@pytest.fixture()
def client(session: Session) -> TestClient:
    app = create_app()

    def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def hr_token(client: TestClient, seeded: Session) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "hr@acme-hr.com", "password": "hr-password-123"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture()
def auth_headers(hr_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {hr_token}"}


@pytest.fixture()
def manager_headers(client: TestClient, seeded: Session) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "manager@acme-hr.com", "password": "manager-password-123"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def ceo_headers(client: TestClient, seeded: Session) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "ceo@acme-hr.com", "password": "ceo-password-123"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
