"""Seed the three role accounts from .env. Safe to run repeatedly."""

from __future__ import annotations

from app.core.config import get_settings
from app.db.init_db import seed_users
from app.db.session import SessionLocal


def main() -> None:
    settings = get_settings()
    with SessionLocal() as session:
        created = seed_users(session, settings.seed_accounts())
    print(f"Seeded {created} user(s).")


if __name__ == "__main__":
    main()
