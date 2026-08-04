"""The anchoring worker (P2-E/P2-F, 04 §5.9 boundary 4).

Two scheduled jobs, wired into the app lifespan next to the integrity check:

- **stamp** — build a sha256 Merkle root over the next contiguous run of
  committed `audit_log` rows, submit it to OpenTimestamps, write one
  `merkle_anchor` row with `verified_at` NULL.
- **upgrade** — ask the calendars for anchors Bitcoin has since confirmed and
  fill in `verified_at`.

**This worker reads the chain; it never writes to it.** Appending an audit row
for an anchor would make anchoring self-feeding: every anchor would create a
new row that needs anchoring. The integrity checker follows the same rule.

**Transactions:** the `*_with_session` functions never commit — the caller
owns the transaction. That is what lets the rollback-based `db` test fixture
drive them directly; `run_*` opens a session, calls them, and commits (09 §8).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import AuditLog, MerkleAnchor
from app.services import merkle, ots

logger = logging.getLogger("apichain.anchoring")

# Advisory-lock keys. **Deliberately distinct from `audit_log.append`'s chain
# key**: sharing it would make every anchor run block every state-changing
# request in the system for as long as a calendar submission takes.
#
# Both are `try` locks. A periodic job that cannot get the lock should skip its
# tick — blocking would queue ticks up behind a holder that is doing network
# I/O, holding a connection the whole time. Skipping loses nothing: the range
# is still there next tick.
_STAMP_LOCK_KEY = 4155_4955
# Two-part key, so replicas can upgrade *different* anchors concurrently.
_UPGRADE_LOCK_NAMESPACE = 4155_4956


def try_stamp_lock(db: Session) -> bool:
    """Claim the right to anchor in this transaction. False = someone else is."""
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _STAMP_LOCK_KEY}
        ).scalar_one()
    )


def try_upgrade_lock(db: Session, anchor_id: int) -> bool:
    """Claim one anchor's upgrade in this transaction."""
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :id)"),
            {"ns": _UPGRADE_LOCK_NAMESPACE, "id": anchor_id},
        ).scalar_one()
    )


@dataclass
class AnchorStatus:
    """Module state for the health surface (05 §3.6 wants anchor lag)."""

    anchor_ok: bool = True
    last_run_at: datetime | None = None
    last_anchor_id: int | None = None
    # The audit id a run is blocked on, if any. Without this the health surface
    # would report a clean bill of health while nothing was being anchored.
    waiting_on_audit_id: int | None = None
    last_error: str | None = None


_status = AnchorStatus()


def current_status() -> AnchorStatus:
    return _status


def reset() -> None:
    """Reset module state (tests)."""
    global _status
    _status = AnchorStatus()


def contiguous_run(available_ids: Sequence[int], *, start: int, max_rows: int) -> list[int]:
    """The unbroken run of ids from `start`, stopping at the first gap.

    Pure, and deliberately so: this is the guard against the id-gap hazard.
    `audit_log.id` comes from a sequence allocated *before* commit, so id 102
    can be committed while 101 is still in flight. Taking everything up to
    `max(id)` would anchor 102 and skip 101 forever, and nothing downstream
    would catch it — every anchor would still verify and `verify_chain` walks
    only the rows that exist. Stopping at the gap leaves 101 for a later tick.
    """
    run: list[int] = []
    expected = start
    for audit_id in available_ids:
        if audit_id != expected:
            break  # a gap: everything after it waits for the next run
        run.append(audit_id)
        expected += 1
        if len(run) >= max_rows:
            break
    return run


def _resolve_gap_at_start(start: int, candidates: list[tuple[int, datetime]]) -> int | None:
    """Decide what to do when the next id we owe an anchor is missing.

    A gap is ambiguous: the id may belong to a transaction still in flight, or
    it may be **burned**. PostgreSQL sequences are not transactional, so
    `append()`'s flush consumes an id immediately and a later rollback — a 500
    mid-request, or the known concurrent-idempotency-key race — leaves that id
    permanently absent.

    Waiting unconditionally would turn a one-row problem into a total one:
    anchoring would stop forever and every later record would stay unanchored.
    So we wait only while the gap could still be in flight. Once the oldest
    committed row above it is older than two anchor intervals, any transaction
    that held the id has long since committed or rolled back, and we skip.

    The skip is never silent: it is logged loudly, and the next anchor's range
    visibly does not continue the previous one, so lost coverage cannot be
    mistaken for coverage by anything reading `merkle_anchor`.
    """
    first_id, first_created_at = candidates[0]
    if first_id == start:
        _status.waiting_on_audit_id = None
        return start

    grace = 2 * settings.anchor_interval_seconds
    age = (datetime.now(UTC).replace(tzinfo=None) - first_created_at).total_seconds()
    if age < grace:
        # Probably still committing. Come back next tick.
        _status.waiting_on_audit_id = start
        logger.info("waiting for audit id %s to commit before anchoring", start)
        return None

    logger.warning(
        "audit id(s) %s-%s never committed (rolled back); skipping to %s. "
        "Anchor coverage has a permanent hole there.",
        start,
        first_id - 1,
        first_id,
    )
    _status.waiting_on_audit_id = None
    return first_id


def _next_start(db: Session) -> int | None:
    """The first audit id this run owes an anchor, or None if there are none."""
    last_anchored = db.execute(select(func.max(MerkleAnchor.to_audit_id))).scalar_one_or_none()
    if last_anchored is not None:
        return int(last_anchored) + 1
    first_audit = db.execute(select(func.min(AuditLog.id))).scalar_one_or_none()
    return int(first_audit) if first_audit is not None else None


def stamp_with_session(
    db: Session, *, calendars: Sequence[ots.Calendar] | None = None
) -> MerkleAnchor | None:
    """Anchor the next contiguous run. Returns the new row, or None if there
    was nothing to anchor or no calendar accepted the root. Does not commit.
    """
    _status.last_run_at = datetime.now(UTC)

    if not try_stamp_lock(db):
        # Another replica is mid-run. Normal operation in a multi-replica
        # deployment, so it must NOT flip anchor_ok or set last_error — Phase
        # 5's alarms would fire on a perfectly healthy cluster.
        logger.debug("another replica holds the anchor lock; skipping this tick")
        return None

    start = _next_start(db)
    if start is None:
        return None

    candidates: list[tuple[int, datetime]] = [
        (audit_id, created_at)
        for audit_id, created_at in db.execute(
            select(AuditLog.id, AuditLog.created_at)
            .where(AuditLog.id >= start)
            .order_by(AuditLog.id.asc())
            .limit(settings.anchor_max_rows)
        ).all()
    ]
    if not candidates:
        _status.waiting_on_audit_id = None
        return None

    start = _resolve_gap_at_start(start, candidates)
    if start is None:
        return None

    run = contiguous_run(
        [audit_id for audit_id, _ in candidates], start=start, max_rows=settings.anchor_max_rows
    )
    if not run:
        return None

    rows = list(
        db.execute(
            select(AuditLog.row_hash)
            .where(AuditLog.id.between(run[0], run[-1]))
            .order_by(AuditLog.id.asc())
        ).scalars()
    )
    root = merkle.build_root([merkle.leaf_hash(row_hash) for row_hash in rows])

    # Submit before inserting, so `anchored_at` never claims an anchor no
    # calendar accepted (09 D9). A crash between the two costs one redundant
    # submission and nothing else: the next run re-anchors the same range.
    try:
        proof = ots.stamp(root, calendars=calendars)
    except ots.CalendarUnavailable as exc:
        _status.anchor_ok = False
        _status.last_error = str(exc)
        logger.warning("anchor run failed, will retry next tick: %s", exc)
        return None

    anchor = MerkleAnchor(
        merkle_root=root,
        from_audit_id=run[0],
        to_audit_id=run[-1],
        anchor_target=settings.anchor_target,
        anchor_proof=proof,
        anchored_at=datetime.now(UTC).replace(tzinfo=None),  # naive UTC, as audit_log
    )
    db.add(anchor)
    db.flush()

    _status.anchor_ok = True
    _status.last_error = None
    _status.last_anchor_id = anchor.id
    logger.info("anchored audit rows %s-%s as %s", run[0], run[-1], root.hex()[:16])
    return anchor


def run_stamp(
    session_factory: sessionmaker, *, calendars: Sequence[ots.Calendar] | None = None
) -> int | None:
    """Scheduler entry point: own the session and the transaction.

    Returns the new anchor's id, not the ORM object — the session closes here,
    and handing back a detached instance whose attributes raise on access is a
    trap for every caller.
    """
    with session_factory() as db:
        anchor = stamp_with_session(db, calendars=calendars)
        if anchor is None:
            return None
        db.commit()
        return anchor.id


# --- the upgrade job: pending -> Bitcoin-confirmed (P2-F, 09 §9) -----------

# One tick's worth of work. Bitcoin confirms in hours, so the pending backlog
# is small in practice; the bound just stops a long outage from turning one
# tick into an unbounded run of calendar calls.
_UPGRADE_BATCH = 100


def _pending_anchors(db: Session) -> list[MerkleAnchor]:
    return list(
        db.execute(
            select(MerkleAnchor)
            .where(MerkleAnchor.verified_at.is_(None))
            .order_by(MerkleAnchor.anchored_at.asc())
            .limit(_UPGRADE_BATCH)
        ).scalars()
    )


def upgrade_anchor(
    db: Session, anchor: MerkleAnchor, *, calendars: Sequence[ots.Calendar] | None = None
) -> bool:
    """Attach a Bitcoin attestation to one anchor if there is one to attach.

    False means "still pending", which is the expected answer for hours after
    stamping. Does not commit.
    """
    try:
        upgraded = ots.upgrade(anchor.anchor_proof, calendars=calendars)
    except ots.InvalidProof:
        # A blob we cannot parse will never upgrade. Log it and leave the row
        # alone rather than failing the whole batch around it.
        logger.error("anchor %s has an unparseable proof", anchor.id)
        return False
    if upgraded is None:
        return False

    anchor.anchor_proof = upgraded
    anchor.verified_at = datetime.now(UTC).replace(tzinfo=None)
    db.flush()
    logger.info(
        "anchor %s confirmed in Bitcoin block(s) %s",
        anchor.id,
        ots.bitcoin_block_heights(upgraded),
    )
    return True


def upgrade_pending_with_session(
    db: Session, *, calendars: Sequence[ots.Calendar] | None = None
) -> int:
    """Try to confirm every pending anchor. Returns how many were confirmed.
    Does not commit."""
    return sum(upgrade_anchor(db, anchor, calendars=calendars) for anchor in _pending_anchors(db))


def run_upgrade(
    session_factory: sessionmaker, *, calendars: Sequence[ots.Calendar] | None = None
) -> int:
    """Scheduler entry point.

    One session (and so one transaction, and one advisory lock) per anchor, so
    that a replica which cannot claim an anchor skips just that one rather than
    the whole tick, and one anchor that fails to upgrade never rolls back the
    ones that succeeded.
    """
    with session_factory() as db:
        pending_ids = [anchor.id for anchor in _pending_anchors(db)]

    confirmed = 0
    for anchor_id in pending_ids:
        with session_factory() as db:
            if not try_upgrade_lock(db, anchor_id):
                logger.debug("anchor %s is being upgraded elsewhere; skipping", anchor_id)
                continue
            anchor = db.get(MerkleAnchor, anchor_id)
            # Re-check under the lock: another replica may have confirmed it
            # between our listing and our claim.
            if anchor is None or anchor.verified_at is not None:
                continue
            if upgrade_anchor(db, anchor, calendars=calendars):
                db.commit()
                confirmed += 1
    return confirmed
