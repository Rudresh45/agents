from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import ReportStatus
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.models.candidate import Candidate
from app.db.models.ceo_report import CeoReport, CeoReportItem
from app.schemas.domain import Actor, ReportDTO, ReportItemInput
from app.services.audit_service import AuditService


class ReportService:
    """Persists CEO report snapshots. File building belongs to the report agent."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    def save_report(
        self,
        *,
        run_id: str | None,
        job_id: str | None,
        file_path: str,
        format: str,
        sent_to: str | None,
        items: list[ReportItemInput],
        actor: Actor,
    ) -> ReportDTO:
        if not file_path:
            raise ValidationFailedError("A report file is required")
        if run_id:
            existing = self.session.scalar(
                select(CeoReport).where(CeoReport.run_id == run_id).limit(1)
            )
            if existing is not None:
                # One CEO report per run. A Kestra retry reuses the existing
                # snapshot instead of writing a second report.
                return ReportDTO.model_validate(existing)
        report = CeoReport(
            run_id=run_id,
            job_id=job_id,
            file_path=file_path,
            format=format,
            sent_to=sent_to,
            status=ReportStatus.GENERATED,
            candidate_count=len(items),
            created_by_id=actor.id if actor.type == "user" else None,
        )
        self.session.add(report)
        self.session.flush()

        for item in items:
            self.session.add(
                CeoReportItem(
                    report_id=report.id,
                    candidate_id=item.candidate_id,
                    job_title=item.job_title,
                    candidate_name=item.candidate_name,
                    score=item.score,
                    matched_skills=item.matched_skills,
                    missing_skills=item.missing_skills,
                    summary=item.summary,
                    decision=item.decision,
                )
            )

        self.audit.record(
            actor=actor,
            action="report.generated",
            entity_type="ceo_report",
            entity_id=report.id,
            new_value={
                "run_id": run_id,
                "job_id": job_id,
                "candidates": len(items),
                "format": format,
            },
            run_id=run_id,
        )
        self.session.commit()
        self.session.refresh(report)
        return ReportDTO.model_validate(report)

    def mark_sent(self, report_id: str, *, sent_to: str, actor: Actor) -> ReportDTO:
        report = self._get_model(report_id)
        if report.sent_at is not None:
            return ReportDTO.model_validate(report)
        report.status = ReportStatus.SENT
        report.sent_to = sent_to
        report.sent_at = datetime.now(UTC)
        self.audit.record(
            actor=actor,
            action="report.sent",
            entity_type="ceo_report",
            entity_id=report.id,
            new_value={"sent_to": sent_to},
            run_id=report.run_id,
        )
        self.session.commit()
        self.session.refresh(report)
        return ReportDTO.model_validate(report)

    def mark_failed(self, report_id: str, *, error: str, actor: Actor) -> None:
        report = self._get_model(report_id)
        report.status = ReportStatus.FAILED
        self.audit.record(
            actor=actor,
            action="report.failed",
            entity_type="ceo_report",
            entity_id=report.id,
            new_value={"error": error[:500]},
            run_id=report.run_id,
        )
        self.session.commit()

    def get_by_run(self, run_id: str) -> ReportDTO | None:
        report = self.session.scalar(
            select(CeoReport)
            .where(CeoReport.run_id == run_id)
            .order_by(CeoReport.created_at.desc())
            .limit(1)
        )
        return ReportDTO.model_validate(report) if report else None

    def record_summary(
        self,
        candidate_id: str,
        *,
        summary: str,
        model_name: str,
        prompt_version: str,
        actor: Actor,
        run_id: str | None = None,
    ) -> None:
        """Audit one CEO summary. The text itself is stored on the report item."""
        if self.session.get(Candidate, candidate_id) is None:
            raise NotFoundError("Candidate not found")
        self.audit.record(
            actor=actor,
            action="report.summary_generated",
            entity_type="candidate",
            entity_id=candidate_id,
            new_value={"summary": summary[:400]},
            model_name=model_name,
            prompt_version=prompt_version,
            run_id=run_id,
        )
        self.session.commit()

    def get(self, report_id: str) -> ReportDTO:
        return ReportDTO.model_validate(self._get_model(report_id))

    def get_detail(self, report_id: str) -> dict:
        report = self._get_model(report_id)
        data = ReportDTO.model_validate(report).model_dump()
        data["items"] = [
            {
                "id": item.id,
                "candidate_id": item.candidate_id,
                "job_title": item.job_title,
                "candidate_name": item.candidate_name,
                "score": item.score,
                "matched_skills": item.matched_skills,
                "missing_skills": item.missing_skills,
                "summary": item.summary,
                "decision": item.decision,
            }
            for item in report.items
        ]
        return data

    def list_reports(
        self, *, page: int = 1, page_size: int = 25
    ) -> tuple[list[ReportDTO], int]:
        total = self.session.scalar(select(func.count()).select_from(CeoReport)) or 0
        rows = self.session.scalars(
            select(CeoReport)
            .order_by(CeoReport.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [ReportDTO.model_validate(row) for row in rows], int(total)

    def items(self, report_id: str) -> list[CeoReportItem]:
        self._get_model(report_id)
        return list(
            self.session.scalars(
                select(CeoReportItem).where(CeoReportItem.report_id == report_id)
            ).all()
        )

    def _get_model(self, report_id: str) -> CeoReport:
        report = self.session.get(CeoReport, report_id)
        if report is None:
            raise NotFoundError("Report not found")
        return report
