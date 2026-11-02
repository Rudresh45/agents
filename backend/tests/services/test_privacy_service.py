from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.core.constants import CandidateStatus, DataRequestType
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.schemas.domain import Actor, ApplicationInput
from app.services.candidate_service import CandidateService
from app.services.privacy_service import PrivacyService
from sqlalchemy.orm import Session


def _candidate(session: Session, actor: Actor, job) -> Candidate:
    dto = CandidateService(session).create_from_application(
        ApplicationInput(full_name="P", email="p@example.com", job_id=job.id),
        actor=actor,
    )
    return session.get(Candidate, dto.id)


def test_retention_sweep_anonymises_expired(session: Session, actor: Actor, job):
    candidate = _candidate(session, actor, job)
    candidate.retention_until = date.today() - timedelta(days=1)
    session.commit()

    assert PrivacyService(session).retention_sweep() == 1
    session.refresh(candidate)
    assert candidate.anonymised is True
    assert candidate.status == CandidateStatus.ANONYMISED


def test_retention_sweep_skips_future(session: Session, actor: Actor, job):
    candidate = _candidate(session, actor, job)
    candidate.retention_until = date.today() + timedelta(days=10)
    session.commit()
    assert PrivacyService(session).retention_sweep() == 0
    session.refresh(candidate)
    assert candidate.anonymised is False


def test_access_request_returns_export(session: Session, actor: Actor, job):
    candidate = _candidate(session, actor, job)
    service = PrivacyService(session)
    request = service.create_request(
        candidate_id=candidate.id, request_type=DataRequestType.ACCESS, actor=actor
    )
    export = service.complete_request(request.id, actor=actor)
    assert export["candidate"]["email"] == "p@example.com"
    assert export["candidate"]["id"] == candidate.id


def test_deletion_request_removes_candidate(session: Session, actor: Actor, job):
    candidate = _candidate(session, actor, job)
    service = PrivacyService(session)
    request = service.create_request(
        candidate_id=candidate.id, request_type=DataRequestType.DELETION, actor=actor
    )
    assert service.complete_request(request.id, actor=actor) is None
    assert session.get(Candidate, candidate.id) is None


def test_invalid_request_type(session: Session, actor: Actor, job):
    candidate = _candidate(session, actor, job)
    with pytest.raises(ValidationFailedError):
        PrivacyService(session).create_request(
            candidate_id=candidate.id, request_type="marketing", actor=actor
        )


def test_request_for_missing_candidate(session: Session, actor: Actor):
    with pytest.raises(NotFoundError):
        PrivacyService(session).create_request(
            candidate_id="ghost", request_type=DataRequestType.ACCESS, actor=actor
        )
