"""documents, communications, verification_codes

Phase 3b (11 §6). The content-addressable media ledger, the communications
log (which doubles as the milestone outbox), and HMAC'd enrolment
verification codes, plus the two contact-verified timestamps on `farmers`.

Hand-written: there is no scratch database to autogenerate against.
`test_migrations.py` round-trips it, and the four new enum types are dropped
explicitly on downgrade because `drop_table` leaves them orphaned.

Revision ID: a3b7c9d1e2f4
Revises: c41ded8b7533
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b7c9d1e2f4"
down_revision: str | Sequence[str] | None = "c41ded8b7533"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_ENUMS = ("comm_status", "comm_purpose", "comm_channel", "scan_status")


def upgrade() -> None:
    comm_channel = sa.Enum("sms", "email", name="comm_channel")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column(
            "scan_status",
            sa.Enum("clean", "infected", "pending", name="scan_status"),
            nullable=False,
        ),
        sa.Column("scan_engine", sa.String(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "subject_type", "subject_id", "content_hash", name="uq_documents_subject_content"
        ),
    )

    op.create_table(
        "communications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", comm_channel, nullable=False),
        sa.Column(
            "purpose",
            sa.Enum("verification", "milestone", name="comm_purpose"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("template_key", sa.String(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("locale", sa.String(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("source_audit_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("queued", "sending", "sent", "failed", "skipped", name="comm_status"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communications")),
        sa.UniqueConstraint("source_audit_id", "channel", name="uq_communications_source_channel"),
    )

    op.create_table(
        "verification_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("farmer_id", sa.Integer(), nullable=False),
        sa.Column("channel", comm_channel, nullable=False),
        sa.Column("code_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("communication_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["farmer_id"], ["farmers.id"], name=op.f("fk_verification_codes_farmer_id_farmers")
        ),
        sa.ForeignKeyConstraint(
            ["communication_id"],
            ["communications.id"],
            name=op.f("fk_verification_codes_communication_id_communications"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_codes")),
    )
    op.create_index(
        op.f("ix_verification_codes_farmer_id"), "verification_codes", ["farmer_id"], unique=False
    )

    op.add_column("farmers", sa.Column("phone_verified_at", sa.DateTime(), nullable=True))
    op.add_column("farmers", sa.Column("email_verified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("farmers", "email_verified_at")
    op.drop_column("farmers", "phone_verified_at")
    op.drop_index(op.f("ix_verification_codes_farmer_id"), table_name="verification_codes")
    op.drop_table("verification_codes")
    op.drop_table("communications")
    op.drop_table("documents")
    for name in _NEW_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
