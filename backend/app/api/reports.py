from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, status
from fastapi.responses import FileResponse

from app.agents.ceo_report_agent import generate_report
from app.agents.contracts import AgentContext
from app.api.deps import DbSession, HrOrCeo, HrUser, actor_for
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.schemas.api import (
    CeoReportRequest,
    Page,
    ReportDetailOut,
    ReportGenerateOut,
    make_page,
)
from app.schemas.domain import ReportDTO
from app.services.report_service import ReportService
from app.utils.excel import CeoReportRow
from app.utils.imap_client import ImapClient
from app.utils.llm_factory import get_llm
from app.utils.mailer import Mailer
from app.utils.pdf import build_ceo_report_pdf
from app.utils.storage import Storage

router = APIRouter(prefix="/reports", tags=["reports"])

Reader = HrOrCeo
Manager = HrUser


@router.get("/ceo", response_model=Page[ReportDTO], summary="List CEO reports")
def list_reports(
    user: Reader,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Page[ReportDTO]:
    items, total = ReportService(db).list_reports(page=page, page_size=page_size)
    return make_page(items, total, page, page_size)


@router.get(
    "/ceo/{report_id}",
    response_model=ReportDetailOut,
    summary="Report metadata and snapshot items",
)
def get_report(
    report_id: str, user: Reader, db: DbSession
) -> ReportDetailOut:
    return ReportDetailOut.model_validate(ReportService(db).get_detail(report_id))


@router.get(
    "/ceo/{report_id}/download",
    summary="Download a CEO report",
    response_class=FileResponse,
)
def download_report(
    report_id: str,
    user: Reader,
    db: DbSession,
    format: str | None = Query(default=None, pattern="^(xlsx|pdf)$"),
) -> FileResponse:
    service = ReportService(db)
    report = service.get(report_id)
    storage = Storage(get_settings())
    path = storage.resolve(report.file_path)

    if format == "pdf" and report.format != "pdf":
        detail = service.get_detail(report_id)
        items = detail["items"]
        rows = [
            CeoReportRow(
                candidate_name=item["candidate_name"],
                job_title=item["job_title"],
                score=item["score"],
                matched_skills=item["matched_skills"],
                missing_skills=item["missing_skills"],
                summary=item["summary"] or "",
                decision=item["decision"] or "",
            )
            for item in items
        ]
        per_job: dict[str, int] = {}
        for item in items:
            title = item["job_title"] or "Unassigned"
            per_job[title] = per_job.get(title, 0) + 1
        summary_rows = [
            {
                "job": title,
                "applications": count,
                "shortlisted": count,
                "accepted": count,
                "rejected": 0,
            }
            for title, count in sorted(per_job.items())
        ]
        created = report.created_at.date().isoformat() if report.created_at else ""
        path = (
            storage.root
            / "reports"
            / (report.run_id or "manual")
            / f"ceo_report_{report.id}.pdf"
        )
        build_ceo_report_pdf(
            path,
            title=f"Approved shortlist - {created}".strip(),
            summary_rows=summary_rows,
            rows=rows,
        )

    if not Path(path).is_file():
        raise NotFoundError("Report file is missing")
    media_type = (
        "application/pdf"
        if path.suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return FileResponse(path, media_type=media_type, filename=Path(path).name)


@router.post(
    "/ceo",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate (and optionally email) a CEO report now",
)
def generate_ceo_report(
    payload: CeoReportRequest, user: Manager, db: DbSession
) -> ReportGenerateOut:
    settings = get_settings()
    if payload.send and not settings.ceo_report_email:
        raise ValidationFailedError("CEO_REPORT_EMAIL is not configured")

    ctx = AgentContext.build(
        session=db,
        llm=get_llm(),
        mailer=Mailer(settings),
        imap=ImapClient(settings),
        settings=settings,
        actor=actor_for(user),
        run_id=None,
        dry_run=settings.email_dry_run,
    )
    result = generate_report(ctx, job_ids=payload.job_ids, send=payload.send)
    return ReportGenerateOut(
        report_id=result.data.get("report_id"),
        counts=result.counts,
        errors=result.errors,
        stop=result.stop,
    )
