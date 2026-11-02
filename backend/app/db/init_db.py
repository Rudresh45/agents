"""Database bootstrap helpers. Alembic owns production schema changes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models.user import User


def seed_users(session: Session, accounts: list[dict[str, str]] | None = None) -> int:
    """Idempotently create the role accounts. Never overwrites an existing password."""
    from app.core.config import get_settings

    accounts = accounts if accounts is not None else get_settings().seed_accounts()
    created = 0
    for account in accounts:
        email = account["email"].lower()
        existing = session.query(User).filter(User.email == email).one_or_none()
        if existing:
            continue
        session.add(
            User(
                email=email,
                password_hash=hash_password(account["password"]),
                full_name=account.get("full_name", ""),
                role=account["role"],
            )
        )
        created += 1
    if created:
        session.commit()
    return created
