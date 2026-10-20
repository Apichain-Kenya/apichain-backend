"""rate_limit_events

Phase 5a (14 P5-B, D7). One row per counted event: a failed login, a wrong
current password, a code send, an upload. `key_hash` is a peppered sha256 of
what is limited (an identifier, an address, an account), so the table holds no
phone, username or IP. It sits outside the audit chain on purpose, and rows
older than the longest window are pruned as the limiter runs.

Hand-written; `test_migrations.py` round-trips it.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_limit_events")),
    )
    op.create_index(
        "ix_rate_limit_events_scope_key_at",
        "rate_limit_events",
        ["scope", "key_hash", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_events_scope_key_at", table_name="rate_limit_events")
    op.drop_table("rate_limit_events")
