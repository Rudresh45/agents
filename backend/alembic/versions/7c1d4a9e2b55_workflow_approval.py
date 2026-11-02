"""Workflow approval metadata

Revision ID: 7c1d4a9e2b55
Revises: 0d39f1a7ec86
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c1d4a9e2b55"
down_revision = "0d39f1a7ec86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs", sa.Column("approved_by_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "workflow_runs",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_runs", sa.Column("stage", sa.String(length=40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "stage")
    op.drop_column("workflow_runs", "approved_at")
    op.drop_column("workflow_runs", "approved_by_id")
