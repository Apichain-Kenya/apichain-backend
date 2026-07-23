"""Idempotency-Key on domain writes (P1-H). Honored when present; a replay
returns the stored response and does not re-run the handler (no second audit
row); a same-key/different-body request conflicts."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Farmer, Role, User
from app.services import security


def _seed_operator(engine, phone: str) -> int:
    with Session(engine) as s:
        u = User(
            phone=phone,
            password_hash=security.hash_password("pw"),
            role=Role.operator,
            is_root=False,
            is_active=True,
        )
        s.add(u)
        s.commit()
        return u.id


def _seed_farmer(engine, phone: str) -> int:
    with Session(engine) as s:
        f = Farmer(first_name="A", last_name="B", phone=phone)
        s.add(f)
        s.commit()
        return f.id


def _auth(uid: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {security.create_access_token(sub=uid, role=Role.operator)}"}


def test_replay_returns_stored_response_and_writes_once(client, migrated_engine):
    op = _seed_operator(migrated_engine, "+254700001001")
    farmer = _seed_farmer(migrated_engine, "+254700001002")
    headers = {**_auth(op), "Idempotency-Key": "abc-123"}

    r1 = client.post("/v2/batches", json={"farmer_id": farmer}, headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v2/batches", json={"farmer_id": farmer}, headers=headers)
    assert r2.status_code == 201
    assert r1.json() == r2.json()  # identical stored response replayed

    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from honey_batches")).scalar() == 1
        assert s.execute(text("select count(*) from audit_log")).scalar() == 1


def test_same_key_different_body_conflicts(client, migrated_engine):
    op = _seed_operator(migrated_engine, "+254700001003")
    f1 = _seed_farmer(migrated_engine, "+254700001004")
    f2 = _seed_farmer(migrated_engine, "+254700001005")
    headers = {**_auth(op), "Idempotency-Key": "dup-key"}

    r1 = client.post("/v2/batches", json={"farmer_id": f1}, headers=headers)
    assert r1.status_code == 201
    r2 = client.post("/v2/batches", json={"farmer_id": f2}, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_without_key_each_call_creates_a_batch(client, migrated_engine):
    op = _seed_operator(migrated_engine, "+254700001006")
    farmer = _seed_farmer(migrated_engine, "+254700001007")
    headers = _auth(op)

    assert (
        client.post("/v2/batches", json={"farmer_id": farmer}, headers=headers).status_code == 201
    )
    assert (
        client.post("/v2/batches", json={"farmer_id": farmer}, headers=headers).status_code == 201
    )
    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from honey_batches")).scalar() == 2
