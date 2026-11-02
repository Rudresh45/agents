from __future__ import annotations

from fastapi import APIRouter

from app.api import audit, auth, candidates, drafts, jobs, logs, privacy, reports, workflow

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(workflow.router)
api_router.include_router(jobs.router)
api_router.include_router(candidates.router)
api_router.include_router(drafts.router)
api_router.include_router(reports.router)
api_router.include_router(audit.router)
api_router.include_router(privacy.router)
api_router.include_router(logs.router)
