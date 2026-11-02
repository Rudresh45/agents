from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.security import create_access_token
from app.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

_LIMIT = 5
_WINDOW_SECONDS = 60
_ATTEMPTS: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _enforce_rate_limit(ip: str) -> None:
    settings = get_settings()
    if settings.app_env == "test":
        return
    now = time.monotonic()
    with _LOCK:
        attempts = [stamp for stamp in _ATTEMPTS.get(ip, []) if now - stamp < _WINDOW_SECONDS]
        if len(attempts) >= _LIMIT:
            _ATTEMPTS[ip] = attempts
            raise RateLimitedError()
        attempts.append(now)
        _ATTEMPTS[ip] = attempts


@router.post("/login", response_model=TokenResponse, summary="Log in and receive a JWT")
def login(payload: LoginRequest, db: DbSession, request: Request) -> TokenResponse:
    client = request.client.host if request.client else "unknown"
    _enforce_rate_limit(client)
    user = AuthService(db).authenticate(str(payload.email), payload.password)
    token, expires_in = create_access_token(subject=user.id, role=user.role)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
