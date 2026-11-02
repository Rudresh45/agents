from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import HrUser
from app.core.config import get_settings
from app.schemas.api import LogTailOut

router = APIRouter(tags=["system"])


@router.get("/logs", response_model=LogTailOut, summary="Tail the application log")
def tail_logs(
    user: HrUser,
    level: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> LogTailOut:
    settings = get_settings()
    path = settings.log_dir / "app.log"
    if not path.is_file():
        return LogTailOut(path=str(path), lines=[])
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()
    if level:
        needle = f'"level": "{level.lower()}"'
        content = [line for line in content if needle in line.lower()]
    return LogTailOut(
        path=str(path),
        lines=[line.rstrip("\n") for line in content[-limit:]],
    )
