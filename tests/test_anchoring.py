"""The anchoring worker: range selection and the stamp job (P2-E, 09 §8).

The range selector is pure and tested directly, because the failure it guards
against is invisible everywhere else. `audit_log.id` is allocated from a
sequence *before* the transaction commits, so a row can be committed while a
lower id is still in flight. A worker that took `id > last` and anchored up to
`max(id)` would skip the in-flight row permanently — and nothing would notice:
every anchor would still be individually valid and `verify_chain` would still
pass, because it walks whatever rows exist.
"""

import hashlib

import pytest

from app.models import AuditLog, MerkleAnchor
from app.services import anchoring, audit_log, merkle
from tests.fakes import FakeCalendar


@pytest.fixture(autouse=True)
def _reset_status():
    anchoring.reset()
    yield
    anchoring.reset()


def _append(db, n: int) -> AuditLog:
    row = audit_log.append(
        db,
        actor_id=None,
        actor_role="admin",
        subject_type="batch",
        subject_id=str(n),
        action="batch.created",
        payload={"n": n},
    )
    db.flush()
    return row


def _expected_root(rows: list[AuditLog]) -> bytes:
    return merkle.build_root([merkle.leaf_hash(r.row_hash) for r in rows])


# --- the contiguous-run selector (pure) ------------------------------------


def test_first_run_starts_at_the_lowest_available_id():
    assert anchoring.contiguous_run([4, 5, 6], start=4, max_rows=100) == [4, 5, 6]


def test_a_run_stops_at_the_first_gap():
    # id 3 is still in flight: anchor 1 and 2 now, pick 3 up on a later tick.
    assert anchoring.contiguous_run([1, 2, 4, 5], start=1, max_rows=100) == [1, 2]


def test_a_gap_at_the_very_start_yields_nothing():
    # The next row we owe an anchor is 3, and 3 is not committed yet. Anchoring
    # 4 and 5 now would strand 3 forever.
    assert anchoring.contiguous_run([4, 5], start=3, max_rows=100) == []


def test_nothing_new_yields_nothing():
    assert anchoring.contiguous_run([], start=1, max_rows=100) == []


def test_the_cap_truncates_the_run_but_keeps_it_contiguous():
    run = anchoring.contiguous_run([1, 2, 3, 4, 5], start=1, max_rows=2)
    assert run == [1, 2]


def test_ids_are_never_reordered_or_deduplicated():
    assert anchoring.contiguous_run([1, 2, 3], start=1, max_rows=100) == [1, 2, 3]


# --- the stamp job (DB) -----------------------------------------------------


def test_a_run_anchors_every_new_audit_row(db):
    rows = [_append(db, i) for i in range(3)]

    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchor is not None
    assert anchor.from_audit_id == rows[0].id
    assert anchor.to_audit_id == rows[-1].id
    assert anchor.merkle_root == _expected_root(rows)
    assert anchor.anchored_at is not None
    assert anchor.verified_at is None  # pending until Bitcoin confirms


def test_the_stored_proof_commits_to_the_stored_root(db):
    from app.services import ots

    _append(db, 0)
    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchor is not None
    assert ots.message(anchor.anchor_proof) == anchor.merkle_root


def test_a_run_with_no_new_rows_writes_nothing(db):
    _append(db, 0)
    anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchoring.stamp_with_session(db, calendars=[FakeCalendar()]) is None
    assert db.query(MerkleAnchor).count() == 1


def test_the_next_run_starts_after_the_previous_range(db):
    first = [_append(db, i) for i in range(2)]
    anchoring.stamp_with_session(db, calendars=[FakeCalendar()])
    second = [_append(db, i) for i in range(2, 4)]

    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchor is not None
    assert anchor.from_audit_id == first[-1].id + 1
    assert anchor.to_audit_id == second[-1].id


def test_a_calendar_outage_writes_nothing_and_the_next_run_recovers(db):
    rows = [_append(db, i) for i in range(2)]

    assert anchoring.stamp_with_session(db, calendars=[FakeCalendar(down=True)]) is None
    assert db.query(MerkleAnchor).count() == 0
    assert anchoring.current_status().anchor_ok is False
    assert anchoring.current_status().last_error is not None

    # The scheduler is the retry: the same range is recomputed next tick.
    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])
    assert anchor is not None
    assert (anchor.from_audit_id, anchor.to_audit_id) == (rows[0].id, rows[-1].id)
    assert anchoring.current_status().anchor_ok is True


def test_anchoring_never_writes_to_the_chain_it_anchors(db):
    """Otherwise anchoring feeds itself: every anchor creates a row to anchor."""
    _append(db, 0)
    before = db.query(AuditLog).count()

    anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert db.query(AuditLog).count() == before


def test_coverage_stays_gap_free_and_non_overlapping_across_interleaved_runs(db):
    for batch in range(4):
        for i in range(batch + 1):  # 1, 2, 3, 4 rows between runs
            _append(db, i)
        anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    anchors = db.query(MerkleAnchor).order_by(MerkleAnchor.from_audit_id).all()
    assert len(anchors) == 4

    first_audit_id = db.query(AuditLog).order_by(AuditLog.id).first().id
    expected_start = first_audit_id
    for anchor in anchors:
        assert anchor.from_audit_id == expected_start, "gap or overlap between anchors"
        expected_start = anchor.to_audit_id + 1
    assert expected_start - 1 == db.query(AuditLog).order_by(AuditLog.id.desc()).first().id


def test_the_cap_leaves_the_remainder_for_the_next_run(db, monkeypatch):
    import app.config

    monkeypatch.setattr(app.config.settings, "anchor_max_rows", 2)
    rows = [_append(db, i) for i in range(5)]

    first = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])
    second = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert first is not None and second is not None
    assert (first.from_audit_id, first.to_audit_id) == (rows[0].id, rows[1].id)
    assert (second.from_audit_id, second.to_audit_id) == (rows[2].id, rows[3].id)


def test_a_single_row_anchors_as_its_own_root(db):
    row = _append(db, 0)
    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchor is not None
    assert anchor.merkle_root == merkle.leaf_hash(row.row_hash)


def test_the_root_is_not_a_bare_hash_of_the_row_hash(db):
    # Guards the leaf domain separation end to end: a leaf is
    # sha256(0x00 || row_hash), never sha256(row_hash).
    row = _append(db, 0)
    anchor = anchoring.stamp_with_session(db, calendars=[FakeCalendar()])

    assert anchor is not None
    assert anchor.merkle_root != hashlib.sha256(row.row_hash).digest()
