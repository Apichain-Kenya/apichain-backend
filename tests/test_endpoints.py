"""Integration tests for the P1-F endpoints — the Phase 1 acceptance (06).

Exercises the whole spine end to end: an authenticated, role-checked actor makes
a consent-gated protected write and a state-changing write, each producing an
audit_log row, and the two rows chain and verify.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Role, User
from app.services import audit_log as al
from app.services import security


def _seed_user(engine, role: Role, phone: str, password: str = "pw") -> int:
    with Session(engine) as s:
        u = User(
            phone=phone,
            password_hash=security.hash_password(password),
            role=role,
            is_root=False,
            is_active=True,
        )
        s.add(u)
        s.commit()
        return u.id


def _auth(user_id: int, role: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer {security.create_access_token(sub=user_id, role=role)}"}


def _enroll_body(phone: str, granted: bool = True) -> dict:
    return {
        "first_name": "Jane",
        "last_name": "Doe",
        "phone": phone,
        "password": "farmerpass",
        "consent_granted": granted,
        "consent_text_version": "v1",
    }


def test_enroll_then_create_batch_chains_two_audit_rows(client, migrated_engine):
    officer = _seed_user(migrated_engine, Role.field_officer, "+254700000001")
    operator = _seed_user(migrated_engine, Role.operator, "+254700000002")

    r1 = client.post(
        "/v2/farmers",
        json=_enroll_body("+254711111111"),
        headers=_auth(officer, Role.field_officer),
    )
    assert r1.status_code == 201, r1.text
    farmer_id = r1.json()["id"]

    r2 = client.post(
        "/v2/batches", json={"farmer_id": farmer_id}, headers=_auth(operator, Role.operator)
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["state"] == "CREATED"

    with Session(migrated_engine) as s:
        rows = s.execute(
            text("select action, prev_hash, row_hash from audit_log order by id")
        ).all()
        assert [r.action for r in rows] == ["farmer.enrolled", "batch.created"]
        assert rows[1].prev_hash == rows[0].row_hash  # the second row chains to the first
        assert al.verify_chain(s).ok


def test_enroll_without_consent_is_rejected_and_writes_nothing(client, migrated_engine):
    officer = _seed_user(migrated_engine, Role.field_officer, "+254700000003")
    res = client.post(
        "/v2/farmers",
        json=_enroll_body("+254722222222", granted=False),
        headers=_auth(officer, Role.field_officer),
    )
    assert res.status_code == 422
    assert res.json()["code"] == "consent_required"
    with Session(migrated_engine) as s:
        assert s.execute(text("select count(*) from farmers")).scalar() == 0
        assert s.execute(text("select count(*) from users where role='farmer'")).scalar() == 0
        assert s.execute(text("select count(*) from audit_log")).scalar() == 0


def test_operator_may_not_enroll_farmer(client, migrated_engine):
    operator = _seed_user(migrated_engine, Role.operator, "+254700000004")
    res = client.post(
        "/v2/farmers", json=_enroll_body("+254733333333"), headers=_auth(operator, Role.operator)
    )
    assert res.status_code == 403
    assert res.json()["code"] == "forbidden"


def test_create_batch_for_unknown_farmer_is_404(client, migrated_engine):
    operator = _seed_user(migrated_engine, Role.operator, "+254700000005")
    res = client.post(
        "/v2/batches", json={"farmer_id": 9999}, headers=_auth(operator, Role.operator)
    )
    assert res.status_code == 404
    assert res.json()["code"] == "farmer_not_found"


def test_login_refresh_rotation_and_reuse_detection(client, migrated_engine):
    _seed_user(migrated_engine, Role.operator, "+254700000006", password="hunter2")

    login = client.post(
        "/v2/auth/login", json={"identifier": "+254700000006", "password": "hunter2"}
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    rotated = client.post("/v2/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    new_tokens = rotated.json()

    # Replaying the spent refresh token is detected and revokes the family.
    reuse = client.post("/v2/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "token_reuse"

    # The freshly issued token is now revoked too.
    after = client.post("/v2/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]})
    assert after.status_code == 401


def test_login_with_wrong_password_is_401(client, migrated_engine):
    _seed_user(migrated_engine, Role.operator, "+254700000007", password="right")
    res = client.post("/v2/auth/login", json={"identifier": "+254700000007", "password": "nope"})
    assert res.status_code == 401
    assert res.json()["code"] == "invalid_credentials"
