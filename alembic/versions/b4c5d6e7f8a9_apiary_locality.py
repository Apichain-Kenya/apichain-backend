"""apiary_locations.locality

Phase 4a (12 §6, D12). A locality label ("Nyeri, Central, Kenya") derived at
apiary creation by reverse-geocoding coordinates rounded to 2 dp, so the
anonymous consumer view can name a place without showing coordinates. It is a
display label, not part of any canonical or audit payload, and is nullable
because the lookup is fail-soft.

Hand-written; `test_migrations.py` round-trips it.

Revision ID: b4c5d6e7f8a9
Revises: a3b7c9d1e2f4
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b7c9d1e2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("apiary_locations", sa.Column("locality", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("apiary_locations", "locality")
