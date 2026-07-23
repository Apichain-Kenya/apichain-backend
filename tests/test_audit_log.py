"""Tests for the audit_log row-hash chain writer (P1-C), tests-first.

Locks the spine's core invariants: genesis zero-hash, row-to-row chaining,
row_hash recomputation, tamper detection, and that concurrent appenders
serialize into one linear chain (no forked prev_hash).
"""

import threading

from sqlalchemy import text, update

from app.models import AuditLog
from app.services import audit_log as al
from app.services.canonical import compute_data_hash

GENESIS = b"\x00" * 32


def _append(db, *, subject_id="1", actor_id=7, action="batch.created", payload=None):
    return al.append(
        db,
        actor_id=actor_id,
        actor_role="operator",
        subject_type="batch",
        subject_id=subject_id,
        action=action,
        payload=payload if payload is not None else {"batch_code": subject_id},
        ip="127.0.0.1",
        user_agent="pytest",
    )


def test_genesis_row_uses_zero_prev_hash(db):
    row = _append(db, subject_id="1")
    assert row.prev_hash == GENESIS
    assert len(row.row_hash) == 32
    assert row.payload_hash == compute_data_hash({"batch_code": "1"})


def test_second_row_chains_to_first(db):
    r1 = _append(db, subject_id="1")
    r2 = _append(db, subject_id="2")
    assert r2.prev_hash == r1.row_hash
    assert r1.id < r2.id


def test_verify_ok_on_clean_chain(db):
    _append(db, subject_id="1")
    _append(db, subject_id="2")
    _append(db, subject_id="3")
    verdict = al.verify_chain(db)
    assert verdict.ok
    assert verdict.checked == 3
    assert verdict.first_divergent_id is None


def test_verify_detects_payload_tamper(db):
    r1 = _append(db, subject_id="1")
    _append(db, subject_id="2")
    db.execute(update(AuditLog).where(AuditLog.id == r1.id).values(payload={"batch_code": "EVIL"}))
    verdict = al.verify_chain(db)
    assert not verdict.ok
    assert verdict.first_divergent_id == r1.id


def test_verify_detects_row_hash_tamper(db):
    r1 = _append(db, subject_id="1")
    _append(db, subject_id="2")
    db.execute(update(AuditLog).where(AuditLog.id == r1.id).values(row_hash=b"\x11" * 32))
    verdict = al.verify_chain(db)
    assert not verdict.ok
    assert verdict.first_divergent_id == r1.id


def test_concurrent_appends_form_one_linear_chain(migrated_engine):
    """Two appenders racing must serialize on the chain lock and produce a
    linear chain — no two rows sharing a prev_hash (a fork)."""
    start = threading.Barrier(2)

    def worker(subject_id):
        conn = migrated_engine.connect()
        trans = conn.begin()
        from sqlalchemy.orm import Session

        s = Session(bind=conn)
        try:
            start.wait(timeout=5)
            _append(s, subject_id=subject_id)
            trans.commit()
        finally:
            s.close()
            conn.close()

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    verify_conn = migrated_engine.connect()
    try:
        from sqlalchemy.orm import Session

        s = Session(bind=verify_conn)
        rows = s.execute(text("select id, prev_hash, row_hash from audit_log order by id")).all()
        prev_hashes = [r.prev_hash for r in rows]
        assert len(prev_hashes) == len(set(prev_hashes)), "a prev_hash was reused -> chain forked"
        verdict = al.verify_chain(s)
        assert verdict.ok
        s.close()
    finally:
        # clean up the committed rows so other tests keep a pristine table
        verify_conn.execute(text("truncate table audit_log restart identity"))
        verify_conn.commit()
        verify_conn.close()
