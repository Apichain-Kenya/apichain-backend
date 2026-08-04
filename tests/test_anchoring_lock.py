"""Only one replica anchors at a time (09 D15).

Without a lock, two app replicas both run the scheduler, both select the same
contiguous run, both submit the same root to the calendars, and the second
insert hits `ex_merkle_anchor_no_overlap` as an uncaught IntegrityError out of
the job. It self-heals next tick, but it wastes a calendar submission and
throws on every collision.

The lock is a **try**-lock, not a blocking one: a periodic job that cannot get
the lock should skip its tick, not queue up behind a holder that is doing
network I/O to a calendar server. And it is on its own key, never the audit
chain's — sharing that key would serialize anchoring against every audit write
in the system.

Advisory locks are per-session, so these tests use real separate sessions.
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


def test_a_second_replica_skips_its_tick_while_one_holds_the_lock(sessions):
    _commit_row(sessions, "1")

    holder = sessions()  # stands in for the other replica, mid-run
    try:
        assert anchoring.try_stamp_lock(holder) is True

        # The second replica must return cleanly, not raise and not write.
        assert anchoring.stamp_with_session(sessions(), calendars=[FakeCalendar()]) is None
        with sessions() as s:
            assert s.query(MerkleAnchor).count() == 0
    finally:
        holder.rollback()
        holder.close()


def test_the_next_tick_anchors_once_the_holder_finishes(sessions):
    _commit_row(sessions, "1")

    holder = sessions()
    assert anchoring.try_stamp_lock(holder) is True
    holder.rollback()  # transaction ends -> the xact lock releases
    holder.close()

    # Skipping a tick loses nothing: the same range is still waiting.
    assert anchoring.run_stamp(sessions, calendars=[FakeCalendar()]) is not None
    with sessions() as s:
        assert s.query(MerkleAnchor).count() == 1


def test_a_skipped_tick_is_not_reported_as_a_failure(sessions):
    # Another replica doing the work is normal operation, not an outage: it
    # must not flip anchor_ok or set last_error, or Phase 5's alarms will fire
    # on a perfectly healthy two-replica deployment.
    _commit_row(sessions, "1")

    holder = sessions()
    try:
        anchoring.try_stamp_lock(holder)
        anchoring.stamp_with_session(sessions(), calendars=[FakeCalendar()])

        assert anchoring.current_status().anchor_ok is True
        assert anchoring.current_status().last_error is None
    finally:
        holder.rollback()
        holder.close()


def test_anchoring_does_not_block_the_audit_chain_writer(sessions, migrated_engine):
    """The anchor lock must be on its own key.

    Sharing `audit_log.append`'s chain key would make every anchor run block
    every state-changing request in the system for as long as a calendar
    submission takes. This test fails loudly if the keys are ever unified.
    """
    holder = sessions()
    try:
        assert anchoring.try_stamp_lock(holder) is True

        # An audit write must still go through while anchoring holds its lock.
        with Session(migrated_engine) as s:
            audit_log.append(
                s,
                actor_id=None,
                actor_role="admin",
                subject_type="batch",
                subject_id="during-anchor",
                action="batch.created",
                payload={},
            )
            s.commit()
    finally:
        holder.rollback()
        holder.close()


def test_two_replicas_do_not_both_upgrade_the_same_anchor(sessions):
    calendar = FakeCalendar()
    _commit_row(sessions, "1")
    anchoring.run_stamp(sessions, calendars=[calendar])
    with sessions() as s:
        anchor_id = s.query(MerkleAnchor.id).scalar()

    calendar.confirm_at_height = 906_100
    holder = sessions()
    try:
        assert anchoring.try_upgrade_lock(holder, anchor_id) is True

        # The other replica finds the anchor locked and leaves it alone rather
        # than spending a second calendar round-trip on it.
        assert anchoring.run_upgrade(sessions, calendars=[calendar]) == 0
        assert calendar.get_timestamp_calls == []
    finally:
        holder.rollback()
        holder.close()

    assert anchoring.run_upgrade(sessions, calendars=[calendar]) == 1


def test_different_anchors_upgrade_concurrently(sessions):
    """Per-anchor keys, not one global upgrade lock: two replicas working on
    different anchors is exactly the parallelism we want to keep."""
    calendar = FakeCalendar()
    _commit_row(sessions, "1")
    anchoring.run_stamp(sessions, calendars=[calendar])
    _commit_row(sessions, "2")
    anchoring.run_stamp(sessions, calendars=[calendar])
    with sessions() as s:
        first_id = s.query(MerkleAnchor.id).order_by(MerkleAnchor.id).first()[0]

    calendar.confirm_at_height = 906_200
    holder = sessions()
    try:
        anchoring.try_upgrade_lock(holder, first_id)
        # The unlocked one still gets upgraded on this tick.
        assert anchoring.run_upgrade(sessions, calendars=[calendar]) == 1
    finally:
        holder.rollback()
        holder.close()


def test_two_real_concurrent_runs_produce_one_anchor_and_no_error(sessions, migrated_engine):
    """The actual race, with threads rather than a stand-in holder.

    Before the lock this raised IntegrityError from whichever replica lost —
    ex_merkle_anchor_no_overlap firing out of a scheduled job — after both had
    already spent a calendar submission on the same root.
    """
    import threading
    import time

    _commit_row(sessions, "1")
    _commit_row(sessions, "2")

    results: list[object] = []
    errors: list[BaseException] = []

    def race(stagger: float) -> None:
        # The calendar round-trip is the race window. Without a delay here the
        # window is too narrow to collide and this test would pass even with
        # the lock removed — verified by disabling the lock and re-running.
        calendar = FakeCalendar(submit_delay=0.5)
        time.sleep(stagger)
        try:
            results.append(anchoring.run_stamp(sessions, calendars=[calendar]))
        except BaseException as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=race, args=(s,)) for s in (0.0, 0.1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent anchor run raised: {errors!r}"
    with sessions() as s:
        assert s.query(MerkleAnchor).count() == 1, "both replicas anchored the same range"
    # Exactly one did the work; the other skipped cleanly.
    assert sorted(r is None for r in results) == [False, True]
