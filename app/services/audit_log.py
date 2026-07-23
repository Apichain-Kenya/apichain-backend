"""The audit_log row-hash chain writer — the tamper-evidence spine (P1-C).

Every state-changing action appends exactly one row via `append()`, the only
writer. Each row chains to the previous:

    row_hash = keccak256(prev_hash ‖ canonical_bytes(content))

a SINGLE keccak over `prev_hash` (raw 32 bytes) concatenated with the canonical
JSON of the row's content (08 §1, 04 §5.2). `content` is every chained column
except the surrogate `id` (which secures nothing the prev_hash linkage doesn't)
and `row_hash` itself; `created_at` is assigned here so it is covered by the
hash and reconstructable by `verify_chain`.

Appenders serialize on a transaction-scoped advisory lock (08 D9) so the
tail-read + insert is atomic per writer without a SERIALIZABLE retry storm. The
lock releases when the caller's transaction commits or rolls back, so the audit
row commits atomically with the domain write it records.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eth_hash.auto import keccak
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.services.canonical import canonical_bytes, canonical_dt, compute_data_hash

# 32 zero bytes: the prev_hash of the first (genesis) row.
GENESIS_PREV_HASH = b"\x00" * 32

# Fixed key for pg_advisory_xact_lock so every appender contends on one lock —
# a single global chain (08 D7). Shard this key by subject_type only if the
# tail ever becomes a hot spot (04 §7).
_CHAIN_LOCK_KEY = 4155_4954


@dataclass(frozen=True)
class ChainVerdict:
    ok: bool
    checked: int
    first_divergent_id: int | None = None
    reason: str | None = None


def _row_hash(
    prev_hash: bytes,
    *,
    actor_id: int | None,
    actor_role: str,
    subject_type: str,
    subject_id: str,
    action: str,
    payload_hash: bytes,
    payload: dict[str, Any],
    created_at: datetime,
    ip: str | None,
    user_agent: str | None,
) -> bytes:
    content = {
        "prev_hash": prev_hash.hex(),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "action": action,
        "payload_hash": payload_hash.hex(),
        "payload": payload,
        "created_at": canonical_dt(created_at),
        "ip": str(ip) if ip is not None else None,
        "user_agent": user_agent,
    }
    return keccak(prev_hash + canonical_bytes(content))


def append(
    db: Session,
    *,
    actor_id: int | None,
    actor_role: str,
    subject_type: str,
    subject_id: str,
    action: str,
    payload: dict[str, Any],
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append one row to the chain and return it (not yet committed).

    Serializes on the chain lock, reads the tail row_hash, computes this row's
    row_hash, inserts, and flushes to assign the id. The caller commits.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHAIN_LOCK_KEY})
    tail = db.execute(
        select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = tail if tail is not None else GENESIS_PREV_HASH

    payload_hash = compute_data_hash(payload)
    created_at = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, matches the column
    row_hash = _row_hash(
        prev_hash,
        actor_id=actor_id,
        actor_role=actor_role,
        subject_type=subject_type,
        subject_id=subject_id,
        action=action,
        payload_hash=payload_hash,
        payload=payload,
        created_at=created_at,
        ip=ip,
        user_agent=user_agent,
    )

    row = AuditLog(
        prev_hash=prev_hash,
        actor_id=actor_id,
        actor_role=actor_role,
        subject_type=subject_type,
        subject_id=subject_id,
        action=action,
        payload_hash=payload_hash,
        payload=payload,
        created_at=created_at,
        ip=ip,
        user_agent=user_agent,
        row_hash=row_hash,
    )
    db.add(row)
    db.flush()
    return row


def verify_chain(
    db: Session, *, from_id: int | None = None, to_id: int | None = None
) -> ChainVerdict:
    """Walk the chain in id order, recomputing every link. Returns the first
    divergence (or ok). Shared by the scheduled integrity job and the health
    endpoint (P1-G)."""
    stmt = select(AuditLog).order_by(AuditLog.id.asc())
    if from_id is not None:
        stmt = stmt.where(AuditLog.id >= from_id)
    if to_id is not None:
        stmt = stmt.where(AuditLog.id <= to_id)
    rows = list(db.execute(stmt).scalars())

    if from_id is not None:
        prior = db.execute(
            select(AuditLog.row_hash)
            .where(AuditLog.id < from_id)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        expected_prev = prior if prior is not None else GENESIS_PREV_HASH
    else:
        expected_prev = GENESIS_PREV_HASH

    checked = 0
    for row in rows:
        checked += 1
        if row.prev_hash != expected_prev:
            return ChainVerdict(False, checked, row.id, "prev_hash breaks linkage")
        if compute_data_hash(row.payload) != row.payload_hash:
            return ChainVerdict(False, checked, row.id, "payload_hash mismatch")
        recomputed = _row_hash(
            row.prev_hash,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            action=row.action,
            payload_hash=row.payload_hash,
            payload=row.payload,
            created_at=row.created_at,
            ip=row.ip,
            user_agent=row.user_agent,
        )
        if recomputed != row.row_hash:
            return ChainVerdict(False, checked, row.id, "row_hash mismatch")
        expected_prev = row.row_hash

    return ChainVerdict(True, checked, None, None)
