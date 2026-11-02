from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AuthError
from app.core.security import verify_internal_token
from app.db.session import get_db
from app.utils.imap_client import ImapClient
from app.utils.mailer import Mailer

internal_bearer = HTTPBearer(auto_error=False, scheme_name="InternalServiceToken")


def require_internal_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(internal_bearer)
    ],
) -> str:
    """Reject missing or wrong service tokens; never fall back to a user JWT."""
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing internal service token")
    if not verify_internal_token(credentials.credentials):
        raise AuthError("Invalid internal service token")
    return credentials.credentials


InternalService = Annotated[str, Depends(require_internal_token)]
DbSession = Annotated[Session, Depends(get_db)]


def get_mailer() -> Mailer:
    return Mailer(get_settings())


def get_imap_client() -> ImapClient:
    return ImapClient(get_settings())


MailerDep = Annotated[Mailer, Depends(get_mailer)]
ImapDep = Annotated[ImapClient, Depends(get_imap_client)]
