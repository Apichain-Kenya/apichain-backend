"""Idempotency-Key on domain writes (P1-H). Honored when present; a replay
returns the stored response and does not re-run the handler (no second audit
row); a same-key/different-body request conflicts.

P3-E made batch creation write three audit rows instead of one, so the "writes
once" assertions count three rather than one — the number that matters is that
a replay adds none of them.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.enums import Role
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user


def _batch_body(farmer_id: int, apiary_id: int) -> dict:
    return {"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA}


def test_replay_returns_stored_response_and_writes_once(client, migrated_engine):
    op = seed_user(migrated_engine, Role.operator, "idem-op-1")
    farmer = seed_farmer(migrated_engine, "+254700001002")
    apiary = seed_apiary(migrated_engine, farmer)
    headers = {**auth(op, Role.operator), "Idempotency-Key": "abc-123"}
    body = _batch_body(farmer, apiary)

    r1 = client.post("/v2/batches", json=body, headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v2/batches", json=body, headers=headers)
    assert r2.status_code == 201
    assert r1.json() == r2.json()  # identical stored response replayed

    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from honey_batches")).scalar() == 1
        assert s.execute(text("select count(*) from audit_log")).scalar() == 3


def test_same_key_different_body_conflicts(client, migrated_engine):
    op = seed_user(migrated_engine, Role.operator, "idem-op-2")
    f1 = seed_farmer(migrated_engine, "+254700001004")
    f2 = seed_farmer(migrated_engine, "+254700001005")
    a1 = seed_apiary(migrated_engine, f1)
    a2 = seed_apiary(migrated_engine, f2)
    headers = {**auth(op, Role.operator), "Idempotency-Key": "dup-key"}

    r1 = client.post("/v2/batches", json=_batch_body(f1, a1), headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v2/batches", json=_batch_body(f2, a2), headers=headers)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_without_key_each_call_creates_a_batch(client, migrated_engine):
    op = seed_user(migrated_engine, Role.operator, "idem-op-3")
    farmer = seed_farmer(migrated_engine, "+254700001007")
    apiary = seed_apiary(migrated_engine, farmer)
    headers = auth(op, Role.operator)
    body = _batch_body(farmer, apiary)

    assert client.post("/v2/batches", json=body, headers=headers).status_code == 201
    assert client.post("/v2/batches", json=body, headers=headers).status_code == 201
    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from honey_batches")).scalar() == 2
