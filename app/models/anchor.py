"""Periodic Merkle anchors over the audit chain (P2-C, 04 §5.2).

One row per anchor run: a sha256 Merkle root over a contiguous run of
`audit_log` rows, published to a public chain. Only the root is published —
raw records never leave PostgreSQL (01 §6).

`anchored_at` with `verified_at` NULL is the **pending window**: OpenTimestamps
returns a proof committing to a calendar immediately, and it only becomes
Bitcoin-attested hours later when the upgrade job (P2-F) fills it in. That gap
is the designed, honest "recorded, public anchor in progress" state the
consumer view renders as its third badge (03 §6), not a failure.

Inclusion proofs are derived on demand from `audit_log`, never stored per row
(04 §5.2), so this table stays one row per anchor no matter how many records it
covers.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    LargeBinary,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import AnchorTarget


class MerkleAnchor(Base):
    __tablename__ = "merkle_anchor"
    __table_args__ = (
        CheckConstraint("from_audit_id <= to_audit_id", name="range"),
        # An audit row covered by two different roots is an ambiguity a proof
        # consumer cannot resolve, so non-overlap is enforced here as well as in
        # the worker's range selection (09 D5). GiST indexes range types
        # natively, so this needs no btree_gist extension.
        ExcludeConstraint(
            (text("int8range(from_audit_id, to_audit_id, '[]')"), "&&"),
            name="ex_merkle_anchor_no_overlap",
            using="gist",
        ),
        # The upgrade job's work queue.
        Index(
            "ix_merkle_anchor_pending",
            "anchored_at",
            postgresql_where=text("verified_at IS NULL"),
        ),
        # "Where did the last run get to?"
        Index("ix_merkle_anchor_to_audit_id", "to_audit_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    merkle_root: Mapped[bytes] = mapped_column(LargeBinary, unique=True)  # sha256, 32 bytes
    # Deliberately NOT foreign keys, for the same reason `audit_log.actor_id`
    # is not one: an anchor is published evidence and must outlive the rows it
    # covers. If audit rows are ever archived out of the hot table (04 §7), the
    # anchor still stands as proof of what was committed and when. The FK would
    # also protect against nothing — audit_log is append-only — while coupling
    # every audit-table maintenance operation to this one.
    from_audit_id: Mapped[int] = mapped_column(BigInteger)  # inclusive
    to_audit_id: Mapped[int] = mapped_column(BigInteger)  # inclusive
    anchor_target: Mapped[AnchorTarget] = mapped_column(Enum(AnchorTarget, name="anchor_target"))
    anchor_proof: Mapped[bytes] = mapped_column(LargeBinary)  # serialized detached .ots
    # Naive UTC, matching audit_log.created_at — mismatched tz handling on two
    # adjacent tables is how an anchor-lag metric ends up off by hours.
    anchored_at: Mapped[datetime] = mapped_column(DateTime)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
