from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AuthError
from app.core.security import hash_password, verify_password
from app.db.models.user import User
from app.schemas.domain import Actor
from app.services.audit_service import AuditService


class AuthService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authenticate(self, email: str, password: str) -> User:
        user = self.session.scalar(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise AuthError("Invalid email or password")
        user.last_login_at = datetime.now(UTC)
        AuditService(self.session).record(
            actor=Actor.from_user(user),
            action="user.login",
            entity_type="user",
            entity_id=user.id,
        )
        self.session.commit()
        self.session.refresh(user)
        return user

    def seed(self, accounts: list[dict[str, str]]) -> int:
        created = 0
        for account in accounts:
            email = account["email"].strip().lower()
            if self.session.scalar(select(User).where(User.email == email)) is not None:
                continue
            self.session.add(
                User(
                    email=email,
                    password_hash=hash_password(account["password"]),
                    full_name=account.get("full_name", ""),
                    role=account["role"],
                )
            )
            created += 1
        if created:
            self.session.commit()
        return created
