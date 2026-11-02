"""Link workflow runs to Kestra executions

Revision ID: 9b2e6d4c8a11
Revises: 7c1d4a9e2b55
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9b2e6d4c8a11"
down_revision = "7c1d4a9e2b55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("kestra_execution_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_workflow_runs_kestra_execution_id",
        "workflow_runs",
        ["kestra_execution_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_kestra_execution_id", table_name="workflow_runs")
    op.drop_column("workflow_runs", "kestra_execution_id")
