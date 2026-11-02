from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import DbSession
from app.services.health_service import HealthService

router = APIRouter(tags=["system"])


@router.get("/health", summary="Component health (database, IMAP, SMTP, LLM)")
def health(db: DbSession) -> JSONResponse:
    result = HealthService(db).check_all()
    status_code = 503 if result["status"] == "unhealthy" else 200
    return JSONResponse(status_code=status_code, content=result)
