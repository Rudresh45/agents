from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ForbiddenError
from app.core.security import decode_token
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.domain import Actor
from app.utils.kestra_client import KestraClient

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token")
    payload = decode_token(credentials.credentials)
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise AuthError("User no longer exists or is inactive")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: str):
    def _dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise ForbiddenError(f"Requires one of roles: {', '.join(roles)}")
        return user

    return _dependency


def get_kestra_client() -> Iterator[KestraClient]:
    """Overridable in tests; production reads URL/token from settings."""
    client = KestraClient()
    try:
        yield client
    finally:
        client.close()


KestraDep = Annotated[KestraClient, Depends(get_kestra_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

HrUser = Annotated[User, Depends(require_role("hr"))]
HrManagerUser = Annotated[User, Depends(require_role("hr_manager"))]
HrOrManager = Annotated[User, Depends(require_role("hr", "hr_manager"))]
HrOrCeo = Annotated[User, Depends(require_role("hr", "ceo"))]
StaffUser = Annotated[User, Depends(require_role("hr", "hr_manager", "ceo"))]


def actor_for(user: User) -> Actor:
    return Actor.from_user(user)
