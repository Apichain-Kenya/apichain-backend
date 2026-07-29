"""On-demand inclusion proofs (P2-G, 04 §5.2, §5.4).

Proofs are **derived when asked for, never stored per audit row** (04 §5.2), so
`merkle_anchor` stays one row per anchor however many records it covers. The
tree is rebuilt from `audit_log` for the anchor's range: at smallholder volumes
that is a sub-millisecond loop, and if it ever stops being one the answer is a
cache, not a schema change.

Every anchor's tree is rebuilt at most once per request, no matter how many of
its rows the batch touches.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, MerkleAnchor
from app.services import merkle
from app.services.merkle import ProofStep


@dataclass(frozen=True)
class ProofEntry:
    """One audit row, with its proof if an anchor covers it yet."""

    audit_id: int
    action: str
    row_hash: bytes
    anchor: MerkleAnchor | None
    path: list[ProofStep] | None

    @property
    def status(self) -> str:
        if self.anchor is None:
            return "pending"  # in our log, no public anchor yet
        return "confirmed" if self.anchor.verified_at is not None else "anchored"


def _covering_anchors(db: Session, first_id: int, last_id: int) -> list[MerkleAnchor]:
    return list(
        db.execute(
            select(MerkleAnchor)
            .where(MerkleAnchor.to_audit_id >= first_id)
            .where(MerkleAnchor.from_audit_id <= last_id)
            .order_by(MerkleAnchor.from_audit_id.asc())
        ).scalars()
    )


def _leaves_for(db: Session, anchor: MerkleAnchor) -> tuple[list[bytes], dict[int, int]]:
    """The anchor's leaves in audit-id order, plus audit_id -> leaf index."""
    rows = list(
        db.execute(
            select(AuditLog.id, AuditLog.row_hash)
            .where(AuditLog.id.between(anchor.from_audit_id, anchor.to_audit_id))
            .order_by(AuditLog.id.asc())
        ).all()
    )
    leaves = [merkle.leaf_hash(row_hash) for _, row_hash in rows]
    index_of = {audit_id: i for i, (audit_id, _) in enumerate(rows)}
    return leaves, index_of


def entries_for_subject(db: Session, *, subject_type: str, subject_id: str) -> list[ProofEntry]:
    """Every audit row for one subject, in chain order, each with its proof."""
    rows = list(
        db.execute(
            select(AuditLog.id, AuditLog.action, AuditLog.row_hash)
            .where(AuditLog.subject_type == subject_type)
            .where(AuditLog.subject_id == subject_id)
            .order_by(AuditLog.id.asc())
        ).all()
    )
    if not rows:
        return []

    anchors = _covering_anchors(db, rows[0][0], rows[-1][0])
    trees: dict[int, tuple[list[bytes], dict[int, int]]] = {}

    entries: list[ProofEntry] = []
    for audit_id, action, row_hash in rows:
        anchor = next((a for a in anchors if a.from_audit_id <= audit_id <= a.to_audit_id), None)
        path = None
        if anchor is not None:
            if anchor.id not in trees:
                trees[anchor.id] = _leaves_for(db, anchor)
            leaves, index_of = trees[anchor.id]
            path = merkle.inclusion_proof(leaves, index_of[audit_id])
        entries.append(ProofEntry(audit_id, action, row_hash, anchor, path))
    return entries


def rollup(entries: list[ProofEntry]) -> str:
    """One badge for the whole batch, without the client walking the list."""
    statuses = {entry.status for entry in entries}
    if not statuses or statuses == {"pending"}:
        return "pending"
    if statuses == {"confirmed"}:
        return "confirmed"
    if "pending" in statuses:
        return "partial"  # some records anchored, some still only in our log
    return "anchored"
