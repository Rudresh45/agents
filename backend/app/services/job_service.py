from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import JobStatus
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.models.job import Job
from app.schemas.domain import Actor, JobDTO, JobInput, JobUpdate
from app.services.audit_service import AuditService


class JobService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.settings = get_settings()

    def get(self, job_id: str) -> JobDTO:
        job = self.session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return JobDTO.model_validate(job)

    def open_jobs(self) -> list[JobDTO]:
        rows = self.session.scalars(
            select(Job).where(Job.status == JobStatus.OPEN).order_by(Job.created_at.asc())
        ).all()
        return [JobDTO.model_validate(row) for row in rows]

    def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[JobDTO], int]:
        filters = []
        if status:
            filters.append(Job.status == status)
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(Job.title.ilike(pattern), Job.location.ilike(pattern))
            )
        total = self.session.scalar(
            select(func.count()).select_from(Job).where(*filters)
        ) or 0
        rows = self.session.scalars(
            select(Job)
            .where(*filters)
            .order_by(Job.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [JobDTO.model_validate(row) for row in rows], int(total)

    def create(self, data: JobInput, *, actor: Actor) -> JobDTO:
        self._validate_alias(data.email_alias, exclude_id=None)
        job = Job(
            title=data.title.strip(),
            description=data.description,
            required_skills=[skill.strip() for skill in data.required_skills if skill.strip()],
            preferred_skills=[
                skill.strip() for skill in data.preferred_skills if skill.strip()
            ],
            min_experience_years=data.min_experience_years,
            education=data.education,
            location=data.location,
            min_score=(
                data.min_score
                if data.min_score is not None
                else self.settings.default_min_score
            ),
            email_alias=data.email_alias.strip().lower() if data.email_alias else None,
            status=JobStatus.OPEN,
            created_by_id=actor.id if actor.type == "user" else None,
        )
        if not job.required_skills:
            raise ValidationFailedError("At least one required skill is needed")
        self.session.add(job)
        self.session.flush()
        self.audit.record(
            actor=actor,
            action="job.created",
            entity_type="job",
            entity_id=job.id,
            new_value={"title": job.title, "min_score": job.min_score},
        )
        self.session.commit()
        self.session.refresh(job)
        return JobDTO.model_validate(job)

    def update(self, job_id: str, data: JobUpdate, *, actor: Actor) -> JobDTO:
        job = self.session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        if job.status == JobStatus.CLOSED:
            raise ValidationFailedError("Cannot edit a closed job")

        changes = data.model_dump(exclude_unset=True)
        if "email_alias" in changes:
            self._validate_alias(changes["email_alias"], exclude_id=job.id)
            if changes["email_alias"]:
                changes["email_alias"] = changes["email_alias"].strip().lower()
        if "required_skills" in changes:
            skills = [skill.strip() for skill in changes["required_skills"] if skill.strip()]
            if not skills:
                raise ValidationFailedError("At least one required skill is needed")
            changes["required_skills"] = skills

        old = {key: getattr(job, key) for key in changes}
        for key, value in changes.items():
            setattr(job, key, value)

        self.audit.record(
            actor=actor,
            action="job.updated",
            entity_type="job",
            entity_id=job.id,
            old_value=old,
            new_value=changes,
        )
        self.session.commit()
        self.session.refresh(job)
        return JobDTO.model_validate(job)

    def close(self, job_id: str, *, actor: Actor) -> JobDTO:
        job = self.session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        if job.status == JobStatus.CLOSED:
            return JobDTO.model_validate(job)
        job.status = JobStatus.CLOSED
        job.closed_at = datetime.now(UTC)
        self.audit.record(
            actor=actor,
            action="job.closed",
            entity_type="job",
            entity_id=job.id,
            old_value={"status": JobStatus.OPEN},
            new_value={"status": JobStatus.CLOSED},
        )
        self.session.commit()
        self.session.refresh(job)
        return JobDTO.model_validate(job)

    def _validate_alias(self, alias: str | None, *, exclude_id: str | None) -> None:
        if not alias:
            return
        query = select(Job).where(func.lower(Job.email_alias) == alias.strip().lower())
        if exclude_id:
            query = query.where(Job.id != exclude_id)
        if self.session.scalar(query.limit(1)) is not None:
            raise ValidationFailedError("This email alias is already used by another job")
