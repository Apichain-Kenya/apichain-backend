"""The `merkle_anchor` table and its range invariants (P2-C, 09 §6).

The worker selects a contiguous run and never re-anchors a covered row (P2-E),
but a scheduling bug, a manual insert, or a future second writer could still
produce overlapping ranges — and an audit row covered by two different roots is
exactly the ambiguity a proof consumer cannot resolve. So the invariant is
enforced in the database as well as in the worker (09 D5).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.enums import AnchorTarget
from app.models import AuditLog, MerkleAnchor
from app.services import audit_log


def _audit_rows(db, count: int) -> list[AuditLog]:
    rows = [
        audit_log.append(
            db,
            actor_id=None,
            actor_role="admin",
            subject_type="batch",
            subject_id=str(i),
            action="batch.created",
            payload={"n": i},
        )
        for i in range(count)
    ]
    db.flush()
    return rows


def _anchor(rows, first: int, last: int, root: bytes) -> MerkleAnchor:
    return MerkleAnchor(
        merkle_root=root,
        from_audit_id=rows[first].id,
        to_audit_id=rows[last].id,
        anchor_target=AnchorTarget.opentimestamps,
        anchor_proof=b"stand-in-ots-proof",
        anchored_at=datetime.now(UTC).replace(tzinfo=None),
    )


def test_an_anchor_row_persists_with_a_pending_window(db):
    rows = _audit_rows(db, 2)
    anchor = _anchor(rows, 0, 1, b"\x11" * 32)
    db.add(anchor)
    db.flush()

    assert anchor.id is not None
    # anchored_at set with verified_at NULL *is* the "not yet anchored" window
    # the consumer view renders as its third state (03 §6).
    assert anchor.anchored_at is not None
    assert anchor.verified_at is None


def test_adjacent_ranges_are_allowed(db):
    rows = _audit_rows(db, 4)
    db.add(_anchor(rows, 0, 1, b"\x11" * 32))
    db.add(_anchor(rows, 2, 3, b"\x22" * 32))
    db.flush()  # no exception: [1,2] and [3,4] touch but do not overlap


def test_overlapping_ranges_are_refused_by_the_database(db):
    rows = _audit_rows(db, 4)
    db.add(_anchor(rows, 0, 2, b"\x11" * 32))
    db.flush()
    db.add(_anchor(rows, 1, 3, b"\x22" * 32))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.flush()


def test_a_single_row_range_cannot_be_anchored_twice(db):
    rows = _audit_rows(db, 1)
    db.add(_anchor(rows, 0, 0, b"\x11" * 32))
    db.flush()
    db.add(_anchor(rows, 0, 0, b"\x22" * 32))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.flush()


def test_an_inverted_range_is_refused(db):
    rows = _audit_rows(db, 2)
    inverted = _anchor(rows, 1, 0, b"\x11" * 32)  # from > to
    db.add(inverted)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.flush()


def test_the_same_root_cannot_be_anchored_twice(db):
    rows = _audit_rows(db, 4)
    db.add(_anchor(rows, 0, 1, b"\x11" * 32))
    db.flush()
    db.add(_anchor(rows, 2, 3, b"\x11" * 32))  # same root, different range
    with pytest.raises(IntegrityError), db.begin_nested():
        db.flush()
