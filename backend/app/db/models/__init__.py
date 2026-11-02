"""Database models. Importing this package registers every table on Base.metadata."""

from app.db.models.audit_log import AuditLog
from app.db.models.candidate import Candidate
from app.db.models.candidate_reply import CandidateReply
from app.db.models.ceo_report import CeoReport, CeoReportItem
from app.db.models.data_request import DataRequest
from app.db.models.email_draft import EmailDraft
from app.db.models.email_log import EmailLog
from app.db.models.job import Job
from app.db.models.user import User
from app.db.models.workflow_run import WorkflowRun

__all__ = [
    "AuditLog",
    "Candidate",
    "CandidateReply",
    "CeoReport",
    "CeoReportItem",
    "DataRequest",
    "EmailDraft",
    "EmailLog",
    "Job",
    "User",
    "WorkflowRun",
]
