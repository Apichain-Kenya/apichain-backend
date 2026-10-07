"""honey_batches public_id

The jar QR identifier (P3-I). Both public views were keyed by the sequential
integer id, so anyone could walk 1..N and scrape every batch. `public_id` is
128 random bits, 32 lowercase hex characters, unique.

Existing rows are backfilled from Python's `secrets`, the same source the
model's default uses, so there is one format and one random source. Added
nullable, filled, then made NOT NULL, so the migration works on a table that
already has rows.

Revision ID: c41ded8b7533
Revises: 4f58f6313266
Create Date: 2026-10-07 12:00:00.000000

"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41ded8b7533"
down_revision: str | Sequence[str] | None = "4f58f6313266"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("honey_batches", sa.Column("public_id", sa.String(length=32), nullable=True))

    conn = op.get_bind()
    ids = conn.execute(sa.text("SELECT id FROM honey_batches")).scalars().all()
    for batch_id in ids:
        conn.execute(
            sa.text("UPDATE honey_batches SET public_id = :pid WHERE id = :id"),
            {"pid": secrets.token_hex(16), "id": batch_id},
        )

    op.alter_column(
        "honey_batches", "public_id", existing_type=sa.String(length=32), nullable=False
    )
    op.create_index(op.f("ix_honey_batches_public_id"), "honey_batches", ["public_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_honey_batches_public_id"), table_name="honey_batches")
    op.drop_column("honey_batches", "public_id")
