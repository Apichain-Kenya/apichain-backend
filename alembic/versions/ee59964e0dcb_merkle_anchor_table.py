"""merkle_anchor table

Revision ID: ee59964e0dcb
Revises: 8547454dfd88
Create Date: 2026-07-28 19:20:06.373879

Hand-reviewed after autogenerate (09 §6): the `anchor_target` enum type is
dropped explicitly on downgrade — `drop_table` leaves it orphaned, which is
exactly the gap `tests/test_migrations.py` asserts against. The audit-id range
bounds are plain columns, not foreign keys: an anchor is published evidence
that must outlive the rows it covers (see app/models/anchor.py).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ee59964e0dcb"
down_revision: str | Sequence[str] | None = "8547454dfd88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "merkle_anchor",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("merkle_root", sa.LargeBinary(), nullable=False),
        sa.Column("from_audit_id", sa.BigInteger(), nullable=False),
        sa.Column("to_audit_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "anchor_target",
            sa.Enum("opentimestamps", "polygon", name="anchor_target"),
            nullable=False,
        ),
        sa.Column("anchor_proof", sa.LargeBinary(), nullable=False),
        sa.Column("anchored_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        # No audit row may be covered by two roots (09 D5). GiST indexes range
        # types natively, so no btree_gist extension is required.
        postgresql.ExcludeConstraint(
            (sa.text("int8range(from_audit_id, to_audit_id, '[]')"), "&&"),
            using="gist",
            name="ex_merkle_anchor_no_overlap",
        ),
        sa.CheckConstraint("from_audit_id <= to_audit_id", name=op.f("ck_merkle_anchor_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merkle_anchor")),
        sa.UniqueConstraint("merkle_root", name=op.f("uq_merkle_anchor_merkle_root")),
    )
    op.create_index(
        "ix_merkle_anchor_pending",
        "merkle_anchor",
        ["anchored_at"],
        unique=False,
        postgresql_where=sa.text("verified_at IS NULL"),
    )
    op.create_index("ix_merkle_anchor_to_audit_id", "merkle_anchor", ["to_audit_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_merkle_anchor_to_audit_id", table_name="merkle_anchor")
    op.drop_index(
        "ix_merkle_anchor_pending",
        table_name="merkle_anchor",
        postgresql_where=sa.text("verified_at IS NULL"),
    )
    op.drop_table("merkle_anchor")
    # drop_table does not remove the enum type it created.
    op.execute("DROP TYPE IF EXISTS anchor_target")
