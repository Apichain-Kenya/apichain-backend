"""Scheduled integrity check + /v2/audit/health (P1-G)."""

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.services import audit_log as al
from app.services import integrity


@pytest.fixture(autouse=True)
def _reset_integrity():
    integrity.reset()
    yield
    integrity.reset()


def _append(session, sid):
    return al.append(
        session,
        actor_id=1,
        actor_role="operator",
        subject_type="batch",
        subject_id=sid,
        action="batch.created",
        payload={"c": sid},
        ip=None,
        user_agent=None,
    )


def test_check_ok_on_clean_chain(db):
    _append(db, "1")
    _append(db, "2")
    status = integrity.check_with_session(db)
    assert status.chain_ok
    assert status.rows_checked == 2
    assert status.checked_at is not None


def test_check_detects_tamper(db):
    r1 = _append(db, "1")
    _append(db, "2")
    db.execute(update(AuditLog).where(AuditLog.id == r1.id).values(payload={"c": "EVIL"}))
    status = integrity.check_with_session(db)
    assert not status.chain_ok
    assert status.first_divergent_id == r1.id


def test_health_endpoint_reports_ok_on_empty_chain(client):
    res = client.get("/v2/audit/health")
    assert res.status_code == 200
    body = res.json()
    assert body["chain_ok"] is True
    assert body["rows_checked"] == 0
    assert body["checked_at"] is not None


def test_health_endpoint_reflects_tamper(client, migrated_engine):
    with Session(migrated_engine) as s:
        r1 = _append(s, "1")
        _append(s, "2")
        s.commit()
        r1_id = r1.id
        s.execute(update(AuditLog).where(AuditLog.id == r1_id).values(payload={"c": "EVIL"}))
        s.commit()
    integrity.reset()  # force the endpoint to run a fresh on-demand check

    res = client.get("/v2/audit/health")
    assert res.status_code == 200
    assert res.json()["chain_ok"] is False
