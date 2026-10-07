"""Two concurrent requests under one Idempotency-Key must not collide (P3-A).

Phase 1 shipped `begin()` as a pure read and `finish()` as an INSERT. Two truly
simultaneous same-key requests therefore both pass `begin()`, and the second
`finish()` duplicates the primary key. Sequential retries were always correct;
only genuine concurrency hit it.

That was carried as a cosmetic known limitation, and it is not one. Look at
where the collision lands in `create_batch`: `audit_log.append()` flushes the
audit row *before* `idempotency.finish()` runs, so the 500 arrives after the
append. PostgreSQL sequences are not transactional, so the rollback burns that
audit id permanently and punches a hole in anchor coverage that Phase 2 can
only skip past, never fill (see `test_anchoring_recovery.py`).

These tests own their sessions and commit for real: the race cannot exist
inside a single transaction that is going to be rolled back. Committed rows are
truncated afterwards so nothing leaks.
"""

import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.errors import APIError
from app.models import IdempotencyKey
from app.services import idempotency

_KEY = "concurrent-key"
_ACTOR = 7
_BODY = {"farmer_id": 1}


@pytest.fixture
def sessions(migrated_engine):
    factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("truncate table idempotency_keys restart identity cascade"))


def _run_concurrently(worker, count=2):
    """Run `worker(n)` in `count` threads released together, and re-raise."""
    barrier = threading.Barrier(count)
    outcomes: list[tuple[int, str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def target(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            outcome = worker(n)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            with lock:
                errors.append(exc)
        else:
            with lock:
                outcomes.append((n, outcome))

    threads = [threading.Thread(target=target, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a worker deadlocked"
    return outcomes, errors


def test_two_concurrent_same_key_requests_yield_one_write_and_one_replay(sessions):
    """The race itself. Neither request may raise, and only one may do the work."""

    def worker(n: int) -> str:
        with sessions() as s:
            handle = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body=_BODY)
            if handle.replay is not None:
                return "replay"
            idempotency.finish(s, handle, status_code=201, body={"winner": n})
            s.commit()
            return "created"

    outcomes, errors = _run_concurrently(worker)

    assert errors == [], f"a concurrent same-key request raised: {errors!r}"
    assert sorted(o for _, o in outcomes) == ["created", "replay"]

    with sessions() as s:
        assert s.execute(select(func.count()).select_from(IdempotencyKey)).scalar_one() == 1


def test_the_replayed_response_is_the_winners_response(sessions):
    """A replay must return what the winner stored, not an empty placeholder."""
    replayed: list[dict] = []

    def worker(n: int) -> str:
        with sessions() as s:
            handle = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body=_BODY)
            if handle.replay is not None:
                replayed.append({"code": handle.replay.status_code, "body": handle.replay.body})
                return "replay"
            idempotency.finish(s, handle, status_code=201, body={"batch_id": 42})
            s.commit()
            return "created"

    _, errors = _run_concurrently(worker)

    assert errors == []
    assert replayed == [{"code": 201, "body": {"batch_id": 42}}]


def test_concurrent_same_key_different_body_is_a_conflict_not_a_crash(sessions):
    """One wins; the other gets the stable 409, never an IntegrityError."""

    def worker(n: int) -> str:
        with sessions() as s:
            try:
                handle = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body={"farmer_id": n})
            except APIError as exc:
                assert exc.status_code == 409
                assert exc.code == "idempotency_conflict"
                return "conflict"
            if handle.replay is not None:
                return "replay"
            idempotency.finish(s, handle, status_code=201, body={"winner": n})
            s.commit()
            return "created"

    outcomes, errors = _run_concurrently(worker)

    assert errors == [], f"a concurrent same-key request raised: {errors!r}"
    assert sorted(o for _, o in outcomes) == ["conflict", "created"]


def test_a_rolled_back_holder_leaves_the_key_free(sessions):
    """A handler that fails after reserving must not burn the key forever."""
    with sessions() as s:
        handle = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body=_BODY)
        assert handle.replay is None
        s.rollback()

    with sessions() as s:
        retry = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body=_BODY)
        assert retry.replay is None, "the abandoned reservation was not released"
        idempotency.finish(s, retry, status_code=201, body={"ok": True})
        s.commit()

    with sessions() as s:
        assert s.execute(select(func.count()).select_from(IdempotencyKey)).scalar_one() == 1


def test_a_reservation_is_invisible_to_other_actors(sessions):
    """The key is scoped to (key, actor_id); another actor is unaffected."""
    with sessions() as s:
        first = idempotency.begin(s, key=_KEY, actor_id=_ACTOR, body=_BODY)
        idempotency.finish(s, first, status_code=201, body={"actor": _ACTOR})
        s.commit()

    with sessions() as s:
        other = idempotency.begin(s, key=_KEY, actor_id=_ACTOR + 1, body=_BODY)
        assert other.replay is None
        idempotency.finish(s, other, status_code=201, body={"actor": _ACTOR + 1})
        s.commit()

    with sessions() as s:
        assert s.execute(select(func.count()).select_from(IdempotencyKey)).scalar_one() == 2
