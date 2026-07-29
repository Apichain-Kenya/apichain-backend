"""A burned audit id must not wedge the worker forever (P2-E, 09 §8.1).

PostgreSQL sequences are not transactional. `audit_log.append()` flushes the
INSERT, which consumes the id there and then. If the caller's transaction later
rolls back — a 500 after the append, or the known concurrent-idempotency-key
race the Phase 1 handoff carries forward — that id is burned and never appears
in the table.

Waiting for it unconditionally would be a worse failure than the id-gap it
guards against: the gap-skip loses one row, but waiting forever stops anchoring
entirely, and every later record stays unanchored with no ceiling.

These tests own their sessions and commit for real, because a burned id cannot
be reproduced inside a transaction that is going to be rolled back anyway. The
committed rows are truncated afterwards so nothing leaks into other tests.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import MerkleAnchor
from app.services import anchoring, audit_log
from tests.fakes import FakeCalendar


@pytest.fixture
def sessions(migrated_engine):
    anchoring.reset()
    factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        anchoring.reset()
        with migrated_engine.begin() as conn:
            conn.execute(text("truncate table merkle_anchor, audit_log restart identity cascade"))


def _commit_row(factory, subject: str) -> int:
    with factory() as s:
        row = audit_log.append(
            s,
            actor_id=None,
            actor_role="admin",
            subject_type="batch",
            subject_id=subject,
            action="batch.created",
            payload={"s": subject},
        )
        s.commit()
        return row.id


def _burn_an_id(factory) -> int:
    """Flush an audit row and roll back — what a 500 mid-request does."""
    with factory() as s:
        row = audit_log.append(
            s,
            actor_id=None,
            actor_role="admin",
            subject_type="batch",
            subject_id="burned",
            action="batch.created",
            payload={},
        )
        burned = row.id
        s.rollback()
        return burned


def _anchor(factory, calendars=None) -> int | None:
    """Run one anchor tick; returns the new anchor's id."""
    return anchoring.run_stamp(factory, calendars=calendars or [FakeCalendar()])


def _load(factory, anchor_id: int) -> MerkleAnchor:
    with factory() as s:
        return s.get(MerkleAnchor, anchor_id)


def test_a_rolled_back_row_burns_its_id(sessions):
    """The premise of everything below, asserted rather than assumed."""
    first = _commit_row(sessions, "1")
    burned = _burn_an_id(sessions)
    later = _commit_row(sessions, "2")

    assert first < burned < later
    with sessions() as s:
        ids = [i for (i,) in s.execute(text("select id from audit_log order by id"))]
    assert burned not in ids


def test_a_recent_gap_is_waited_for_not_skipped(sessions):
    # A gap may just be a transaction still in flight. Anchoring past it would
    # strand a row that is about to commit.
    _commit_row(sessions, "1")
    _anchor(sessions)
    burned = _burn_an_id(sessions)
    _commit_row(sessions, "2")

    assert _anchor(sessions) is None
    assert anchoring.current_status().waiting_on_audit_id == burned


def test_a_long_dead_gap_is_skipped_so_anchoring_keeps_moving(sessions, monkeypatch):
    import app.config

    # Grace is two anchor intervals. At zero, a committed row is already old
    # enough that any lower id can only have been rolled back.
    monkeypatch.setattr(app.config.settings, "anchor_interval_seconds", 0)

    _commit_row(sessions, "1")
    _anchor(sessions)
    _burn_an_id(sessions)
    live_id = _commit_row(sessions, "2")

    anchor_id = _anchor(sessions)

    assert anchor_id is not None, "the worker must not wait forever on an id that never committed"
    assert _load(sessions, anchor_id).from_audit_id == live_id
    assert anchoring.current_status().waiting_on_audit_id is None


def test_the_skip_is_visible_in_the_data_not_just_the_log(sessions, monkeypatch):
    import app.config

    monkeypatch.setattr(app.config.settings, "anchor_interval_seconds", 0)
    _commit_row(sessions, "1")
    first_id = _anchor(sessions)
    _burn_an_id(sessions)
    _commit_row(sessions, "2")
    second_id = _anchor(sessions)

    assert first_id is not None and second_id is not None
    # The second range does not continue the first, so a lost id can never be
    # mistaken for continuous coverage by anything reading the table.
    assert _load(sessions, second_id).from_audit_id > _load(sessions, first_id).to_audit_id + 1


def test_health_reports_the_wait_while_it_lasts(sessions, client):
    _commit_row(sessions, "1")
    _anchor(sessions)
    burned = _burn_an_id(sessions)
    _commit_row(sessions, "2")
    _anchor(sessions)

    body = client.get("/v2/audit/anchor-health").json()

    # The endpoint that exists to detect anchor lag must not report a clean
    # bill of health while nothing is being anchored.
    assert body["waiting_on_audit_id"] == burned
    assert body["pending_rows"] >= 1


# --- the scheduler's own entry points (D11: run_* owns the transaction) ----


def test_run_stamp_commits_its_anchor(sessions, migrated_engine):
    _commit_row(sessions, "1")

    anchor_id = anchoring.run_stamp(sessions, calendars=[FakeCalendar()])

    assert anchor_id is not None
    # Committed, not merely flushed: visible from a separate session.
    with Session(migrated_engine) as s:
        assert s.query(MerkleAnchor).count() == 1


def test_run_upgrade_commits_each_confirmation(sessions, migrated_engine):
    calendar = FakeCalendar()
    _commit_row(sessions, "1")
    anchoring.run_stamp(sessions, calendars=[calendar])

    assert anchoring.run_upgrade(sessions, calendars=[calendar]) == 0  # still pending

    calendar.confirm_at_height = 905_001
    assert anchoring.run_upgrade(sessions, calendars=[calendar]) == 1

    with Session(migrated_engine) as s:
        assert s.query(MerkleAnchor).filter(MerkleAnchor.verified_at.isnot(None)).count() == 1
