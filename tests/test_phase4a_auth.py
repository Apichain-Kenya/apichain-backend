"""P4a-A: the refresh cookie, the reuse window, `GET /v2/me`, and a required
`Idempotency-Key` (12 §5, D3/D5/D7).

The refresh token never appears in a response body: it lives in an HttpOnly
cookie scoped to `/v2/auth`, so no script in the PWA can read it. Refresh and
logout also demand the `X-ApiChain-Client` header, which a cross-site form
cannot send. The reuse window keeps a farmer signed in when a refresh response
is lost on a bad connection, without weakening theft detection for anything
else.
"""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import AuditLog, RefreshToken
from app.services import security
from tests.helpers import auth, seed_farmer, seed_user

COOKIE = "__Secure-apichain_rt"
CLIENT_HEADER = {"X-ApiChain-Client": "pwa"}


def _login(client, identifier="+254700000101", password="hunter2"):
    res = client.post("/v2/auth/login", json={"identifier": identifier, "password": password})
    assert res.status_code == 200, res.text
    return res


def _set_cookie_header(res) -> str:
    headers = [v for k, v in res.headers.multi_items() if k.lower() == "set-cookie"]
    matching = [h for h in headers if h.startswith(COOKIE + "=")]
    assert len(matching) == 1, headers
    return matching[0]


def _cookie_value(res) -> str:
    return _set_cookie_header(res).split(";", 1)[0].split("=", 1)[1]


def _refresh_with(client, raw: str):
    client.cookies.clear()
    return client.post("/v2/auth/refresh", headers=CLIENT_HEADER, cookies={COOKIE: raw})


# --- the cookie ---------------------------------------------------------------


def test_login_sets_an_httponly_secure_strict_cookie_scoped_to_auth(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op101", password="hunter2")
    res = _login(client, "op101")

    attrs = [a.strip().lower() for a in _set_cookie_header(res).split(";")]
    assert "httponly" in attrs
    assert "secure" in attrs
    assert "samesite=strict" in attrs
    assert "path=/v2/auth" in attrs
    assert any(a.startswith("max-age=") and int(a.split("=")[1]) > 0 for a in attrs)


def test_login_body_carries_no_refresh_token(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op102", password="hunter2")
    body = _login(client, "op102").json()

    assert "refresh_token" not in body
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60


def test_refresh_with_cookie_and_header_rotates_the_cookie(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op103", password="hunter2")
    first = _cookie_value(_login(client, "op103"))

    res = _refresh_with(client, first)
    assert res.status_code == 200, res.text
    assert res.json()["access_token"]
    assert "refresh_token" not in res.json()
    assert _cookie_value(res) != first


def test_refresh_without_the_client_header_is_refused(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op104", password="hunter2")
    raw = _cookie_value(_login(client, "op104"))
    client.cookies.clear()

    res = client.post("/v2/auth/refresh", cookies={COOKIE: raw})
    assert res.status_code == 403
    assert res.json()["code"] == "client_header_required"


def test_refresh_without_a_cookie_is_401(client):
    client.cookies.clear()
    res = client.post("/v2/auth/refresh", headers=CLIENT_HEADER)
    assert res.status_code == 401
    assert res.json()["code"] == "invalid_token"


def test_logout_revokes_the_token_and_clears_the_cookie(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op105", password="hunter2")
    raw = _cookie_value(_login(client, "op105"))
    client.cookies.clear()

    res = client.post("/v2/auth/logout", headers=CLIENT_HEADER, cookies={COOKIE: raw})
    assert res.status_code == 200
    assert "max-age=0" in _set_cookie_header(res).lower()

    after = _refresh_with(client, raw)
    assert after.status_code == 401


def test_logout_without_the_client_header_is_refused(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op106", password="hunter2")
    raw = _cookie_value(_login(client, "op106"))
    client.cookies.clear()

    res = client.post("/v2/auth/logout", cookies={COOKIE: raw})
    assert res.status_code == 403
    assert res.json()["code"] == "client_header_required"


# --- the reuse window (D5) ----------------------------------------------------


def test_a_lost_refresh_response_does_not_log_the_user_out(client, migrated_engine):
    """The server rotated A to B, but B never reached the phone. The phone
    presents A again within the window: it gets a working session (C), B is
    retired, and the family survives."""
    seed_user(migrated_engine, Role.operator, "op107", password="hunter2")
    a = _cookie_value(_login(client, "op107"))
    b = _cookie_value(_refresh_with(client, a))  # this response is "lost"

    retry = _refresh_with(client, a)
    assert retry.status_code == 200, retry.text
    c = _cookie_value(retry)
    assert c not in (a, b)

    # C works; the family was not revoked.
    assert _refresh_with(client, c).status_code == 200
    # B, which the phone never had, is retired rather than left live.
    with Session(migrated_engine) as s:
        row_b = s.query(RefreshToken).filter_by(token_hash=security.hash_token(b)).one()
        assert row_b.revoked_at is not None


def test_the_reuse_window_writes_an_audit_row(client, migrated_engine):
    uid = seed_user(migrated_engine, Role.operator, "op108", password="hunter2")
    a = _cookie_value(_login(client, "op108"))
    _refresh_with(client, a)
    assert _refresh_with(client, a).status_code == 200

    with Session(migrated_engine) as s:
        rows = s.query(AuditLog).filter_by(action="auth.refresh_reuse_grace").all()
        assert len(rows) == 1
        assert rows[0].actor_id == uid


def test_reuse_after_the_window_still_revokes_the_family(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "op109", password="hunter2")
    a = _cookie_value(_login(client, "op109"))
    b = _cookie_value(_refresh_with(client, a))
    with Session(migrated_engine) as s:
        s.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == security.hash_token(b))
            .values(issued_at=RefreshToken.issued_at - timedelta(seconds=61))
        )
        s.commit()

    reuse = _refresh_with(client, a)
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "token_reuse"
    assert _refresh_with(client, b).status_code == 401


def test_reuse_when_the_successor_was_already_used_revokes_the_family(client, migrated_engine):
    """A -> B -> C: B was presented, so A coming back is not a lost response."""
    seed_user(migrated_engine, Role.operator, "op110", password="hunter2")
    a = _cookie_value(_login(client, "op110"))
    b = _cookie_value(_refresh_with(client, a))
    c = _cookie_value(_refresh_with(client, b))

    reuse = _refresh_with(client, a)
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "token_reuse"
    assert _refresh_with(client, c).status_code == 401


# --- GET /v2/me ---------------------------------------------------------------


def test_me_for_a_farmer_returns_their_farmer_id(client, migrated_engine):
    farmer_id = seed_farmer(migrated_engine, "+254700000111")
    uid = seed_user(migrated_engine, Role.farmer, "farmer111", farmer_id=farmer_id)

    res = client.get("/v2/me", headers=auth(uid, Role.farmer))
    assert res.status_code == 200, res.text
    assert res.json() == {
        "user_id": uid,
        "role": "farmer",
        "display_name": "A B",
        "farmer_id": farmer_id,
        "must_change_password": False,
    }


def test_me_for_staff_has_no_farmer_id(client, migrated_engine):
    uid = seed_user(migrated_engine, Role.field_officer, "officer112")
    res = client.get("/v2/me", headers=auth(uid, Role.field_officer))
    assert res.status_code == 200
    assert res.json() == {
        "user_id": uid,
        "role": "field_officer",
        "display_name": "officer112",
        "farmer_id": None,
        "must_change_password": False,
    }


def test_me_requires_a_token(client):
    assert client.get("/v2/me").status_code == 401


# --- Idempotency-Key required (D7) --------------------------------------------

_KEYED_WRITES = {
    ("/v2/apiaries", "post"),
    ("/v2/batches", "post"),
    ("/v2/batches/{batch_id}/harvest", "post"),
    ("/v2/batches/{batch_id}/process", "post"),
    ("/v2/batches/{batch_id}/lab-verify", "post"),
    ("/v2/batches/{batch_id}/package", "post"),
    ("/v2/batches/{batch_id}/distribute", "post"),
    ("/v2/farmers", "post"),
    ("/v2/farmers/{farmer_id}/consents", "post"),
}


def test_every_domain_json_write_declares_the_key_required():
    from app.main import app

    spec = app.openapi()
    for path, method in _KEYED_WRITES:
        params = spec["paths"][path][method].get("parameters", [])
        key = [p for p in params if p["in"] == "header" and p["name"] == "Idempotency-Key"]
        assert key and key[0]["required"] is True, (path, method)


def test_a_write_without_the_key_is_refused(client, migrated_engine):
    from app.main import app

    farmer_id = seed_farmer(migrated_engine, "+254700000113")
    uid = seed_user(migrated_engine, Role.farmer, "farmer113", farmer_id=farmer_id)
    bare = TestClient(app, base_url="https://testserver")  # no auto-injected key

    res = bare.post(
        "/v2/apiaries",
        json={"farmer_id": farmer_id, "name": "Ridge", "latitude": "-0.4", "longitude": "36.9"},
        headers=auth(uid, Role.farmer),
    )
    assert res.status_code == 422
    locs = [tuple(e["loc"]) for e in res.json()["detail"]]
    assert ("header", "Idempotency-Key") in locs
