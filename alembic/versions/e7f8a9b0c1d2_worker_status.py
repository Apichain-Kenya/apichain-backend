"""worker_status

Phase 5a (14 P5-D, D9). One row per background job, plus the `worker` row
that carries the heartbeat. Written by the worker process after every run,
read by `/v2/audit/health`, `/v2/audit/anchor-health` and `/health/ops`, so
every API instance reports the same health. Operational state, outside the
audit chain.

Hand-written; `test_migrations.py` round-trips it.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-11-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_status",
        sa.Column("job", sa.String(length=40), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("alarmed", sa.Boolean(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("job", name=op.f("pk_worker_status")),
    )


def downgrade() -> None:
    op.drop_table("worker_status")
