"""idempotency reservation columns nullable

`idempotency.begin()` now reserves `(key, actor_id)` before the handler runs and
`finish()` updates that reservation, so the response columns are empty for the
lifetime of the request (P3-A, `10` §4). Making them nullable is what lets the
reservation exist at all.

A reservation is only ever visible inside its own uncommitted transaction: if the
handler succeeds, `finish()` fills both columns before the commit; if it fails,
the row rolls back with everything else. So no committed row should carry NULLs,
and the downgrade below deletes any that somehow do rather than failing.

Revision ID: cd6efa0b7cd3
Revises: ee59964e0dcb
Create Date: 2026-08-06 14:20:39.815836

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cd6efa0b7cd3"
down_revision: str | Sequence[str] | None = "ee59964e0dcb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("idempotency_keys", "response_code", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "idempotency_keys", "response_body", existing_type=postgresql.JSONB(), nullable=True
    )


def downgrade() -> None:
    # Unfinished reservations cannot satisfy a NOT NULL; they are abandoned
    # rows by definition, so drop them rather than block the downgrade.
    op.execute("delete from idempotency_keys where response_code is null or response_body is null")
    op.alter_column("idempotency_keys", "response_code", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "idempotency_keys", "response_body", existing_type=postgresql.JSONB(), nullable=False
    )
