"""users.full_name and users.must_change_password

Phase 4b (13 §5, D5, D6). `full_name` names a staff member wherever an actor is
shown, so a batch record reads "Jane Mwende" rather than a username (03 §13.1).
`must_change_password` marks an account whose password someone else chose
(enrolment, account creation, an admin reset): until it is changed, every route
but `/v2/me` and `/v2/auth/password` refuses the account. Existing rows get
false, so no current account is locked out by the upgrade.

Hand-written; `test_migrations.py` round-trips it.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=120), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "full_name")
