"""Phase 4b, P4b-A: identity, staff accounts and passwords (13 §5).

The guardrail under test is `03` §13.1: an action is recorded under the person
who took it. That only holds if nobody else knows that person's password, so
the forced change (D5), the password-free fingerprints (D4) and the admin-only
account endpoints are tested together here.
"""

import hashlib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import AuditLog, Farmer, IdempotencyKey, RefreshToken, User
from app.services import bootstrap, security, users
from app.services.canonical import canonical_bytes
from tests.helpers import auth, create_batch, seed_farmer, seed_user

CLIENT = {"X-ApiChain-Client": "pwa"}
COOKIE = "__Secure-apichain_rt"
STAFF_PW = "honey from tetu in march"
NEW_STAFF_PW = "a different long sentence"
FARMER_PW = "farmpass9"


def _staff(
    engine,
    role: Role,
    username: str,
    *,
    password: str = STAFF_PW,
    is_root: bool = False,
    full_name: str | None = None,
    must_change: bool = False,
    active: bool = True,
) -> int:
    with Session(engine) as s:
        user = User(
            username=username,
            full_name=full_name,
            password_hash=security.hash_password(password),
            role=role,
            is_root=is_root,
            is_active=active,
            must_change_password=must_change,
        )
        s.add(user)
        s.commit()
        return user.id


def _user(engine, user_id: int) -> User:
    with Session(engine) as s:
        user = s.get(User, user_id)
        assert user is not None
        s.expunge(user)
        return user


def _login(client, identifier: str, password: str):
    client.cookies.clear()
    return client.post("/v2/auth/login", json={"identifier": identifier, "password": password})


def _cookie(res) -> str:
    header = next(v for k, v in res.headers.multi_items() if k.lower() == "set-cookie")
    return header.split(";", 1)[0].split("=", 1)[1]


def _new_user(**over) -> dict:
    return {
        "username": "jmwende",
        "full_name": "Jane Mwende",
        "role": "operator",
        "password": STAFF_PW,
        **over,
    }


def _enroll_body(phone: str, **over) -> dict:
    return {
        "first_name": "Wanjiru",
        "last_name": "Kamau",
        "phone": phone,
        "password": FARMER_PW,
        "consent_granted": True,
        "consent_text_version": "en-2026-10",
        **over,
    }


def _payloads(engine, action: str) -> list[dict]:
    with Session(engine) as s:
        return [
            r.payload
            for r in s.execute(select(AuditLog).where(AuditLog.action == action)).scalars()
        ]


# --- Bootstrap (D3) ------------------------------------------------------------


def test_bootstrap_creates_one_root_admin_and_audits_without_the_password(client, migrated_engine):
    with Session(migrated_engine) as s:
        user_id = bootstrap.create_root_admin(
            s, username="groot", full_name="Grace Njeri", password=STAFF_PW
        )
        s.commit()
    user = _user(migrated_engine, user_id)
    assert (user.role, user.is_root, user.is_active, user.must_change_password) == (
        Role.admin,
        True,
        True,
        False,
    )
    [payload] = _payloads(migrated_engine, "user.bootstrapped")
    assert payload == {"user_id": user_id, "username": "groot"}
    assert _login(client, "groot", STAFF_PW).status_code == 200


def test_bootstrap_refuses_when_a_root_exists(migrated_engine, client):
    _staff(migrated_engine, Role.admin, "root1", is_root=True)
    with Session(migrated_engine) as s, pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.create_root_admin(s, username="root2", full_name="Two", password=STAFF_PW)


def test_bootstrap_applies_the_staff_password_rule(migrated_engine, client):
    with Session(migrated_engine) as s, pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.create_root_admin(s, username="root3", full_name="Short", password="short")


# --- Staff accounts: who may manage them ---------------------------------------


@pytest.mark.parametrize("role", [Role.farmer, Role.field_officer, Role.operator, Role.lab_officer])
def test_only_an_admin_manages_accounts(client, migrated_engine, role):
    actor = seed_user(migrated_engine, role, f"actor-{role}")
    target = _staff(migrated_engine, Role.operator, f"target-{role}")
    h = auth(actor, role)
    assert client.post("/v2/users", json=_new_user(), headers=h).status_code == 403
    assert client.get("/v2/users", headers=h).status_code == 403
    assert client.post(f"/v2/users/{target}/deactivate", headers=h).status_code == 403
    reset = client.post(
        f"/v2/users/{target}/reset-password", json={"password": NEW_STAFF_PW}, headers=h
    )
    assert reset.status_code == 403


def test_admin_creates_a_staff_account_that_must_change_its_password(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "padmin")
    res = client.post("/v2/users", json=_new_user(), headers=auth(admin, Role.admin))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["username"] == "jmwende"
    assert body["full_name"] == "Jane Mwende"
    assert body["role"] == "operator"
    assert body["is_active"] is True
    assert body["must_change_password"] is True
    assert "password" not in body and "password_hash" not in body
    assert STAFF_PW not in json.dumps(body)

    [payload] = _payloads(migrated_engine, "user.created")
    assert payload == {
        "user_id": body["id"],
        "username": "jmwende",
        "role": "operator",
        "created_by": admin,
    }


def test_only_the_root_admin_creates_another_admin(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "plainadmin")
    root = _staff(migrated_engine, Role.admin, "rootadmin", is_root=True)
    refused = client.post(
        "/v2/users",
        json=_new_user(username="newadmin", role="admin"),
        headers=auth(admin, Role.admin),
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "root_required"
    ok = client.post(
        "/v2/users",
        json=_new_user(username="newadmin", role="admin"),
        headers=auth(root, Role.admin),
    )
    assert ok.status_code == 201, ok.text


def test_a_farmer_account_is_never_made_here(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "fadmin")
    res = client.post("/v2/users", json=_new_user(role="farmer"), headers=auth(admin, Role.admin))
    assert res.status_code == 422


def test_a_taken_username_is_409(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "dupadmin")
    _staff(migrated_engine, Role.operator, "jmwende")
    res = client.post("/v2/users", json=_new_user(), headers=auth(admin, Role.admin))
    assert res.status_code == 409
    assert res.json()["code"] == "username_taken"


@pytest.mark.parametrize("username", ["ab", "0712345678", "+254700000001", "Jane Mwende"])
def test_usernames_start_with_a_letter_and_cannot_look_like_a_phone(
    client, migrated_engine, username
):
    admin = _staff(migrated_engine, Role.admin, f"uadmin{abs(hash(username)) % 1000}")
    res = client.post(
        "/v2/users", json=_new_user(username=username), headers=auth(admin, Role.admin)
    )
    assert res.status_code == 422


def test_admin_lists_staff_only_filtered_and_paged(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "ladmin", full_name="Peter Otieno")
    op = _staff(migrated_engine, Role.operator, "lop", full_name="Jane Mwende")
    _staff(migrated_engine, Role.lab_officer, "llab")
    _staff(migrated_engine, Role.operator, "lgone", active=False)
    farmer_id = seed_farmer(migrated_engine, "+254700004001")
    seed_user(migrated_engine, Role.farmer, "lfarmer", farmer_id=farmer_id)
    h = auth(admin, Role.admin)

    everyone = client.get("/v2/users", headers=h).json()
    names = [u["username"] for u in everyone["items"]]
    assert "lfarmer" not in names
    assert set(names) == {"ladmin", "lop", "llab", "lgone"}
    ids = [u["id"] for u in everyone["items"]]
    assert ids == sorted(ids, reverse=True)  # newest first

    ops = client.get("/v2/users?role=operator&active=true", headers=h).json()["items"]
    assert [u["id"] for u in ops] == [op]

    first = client.get("/v2/users?limit=2", headers=h).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    rest = client.get(f"/v2/users?limit=2&cursor={first['next_cursor']}", headers=h).json()
    assert {u["username"] for u in first["items"] + rest["items"]} == set(names)
    assert rest["next_cursor"] is None


# --- Deactivation --------------------------------------------------------------


def test_deactivation_revokes_sessions_and_is_audited(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "dadmin")
    op = _staff(migrated_engine, Role.operator, "dop")
    raw = _cookie(_login(client, "dop", STAFF_PW))

    res = client.post(f"/v2/users/{op}/deactivate", headers=auth(admin, Role.admin))
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False
    [payload] = _payloads(migrated_engine, "user.deactivated")
    assert payload == {"user_id": op, "deactivated_by": admin}

    client.cookies.clear()
    refreshed = client.post("/v2/auth/refresh", headers=CLIENT, cookies={COOKIE: raw})
    assert refreshed.status_code == 401
    assert client.get("/v2/batches", headers=auth(op, Role.operator)).status_code == 401
    assert _login(client, "dop", STAFF_PW).status_code == 401


def test_deactivating_an_inactive_account_again_is_harmless(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "d2admin")
    op = _staff(migrated_engine, Role.operator, "d2op", active=False)
    res = client.post(f"/v2/users/{op}/deactivate", headers=auth(admin, Role.admin))
    assert res.status_code == 200
    assert _payloads(migrated_engine, "user.deactivated") == []


def test_nobody_deactivates_themselves(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "selfadmin")
    _staff(migrated_engine, Role.admin, "otheradmin")
    res = client.post(f"/v2/users/{admin}/deactivate", headers=auth(admin, Role.admin))
    assert res.status_code == 409
    assert res.json()["code"] == "cannot_deactivate_self"


def test_the_root_admin_cannot_be_deactivated(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "radmin")
    root = _staff(migrated_engine, Role.admin, "rroot", is_root=True)
    res = client.post(f"/v2/users/{root}/deactivate", headers=auth(admin, Role.admin))
    assert res.status_code == 409
    assert res.json()["code"] == "root_protected"


def test_the_last_active_admin_cannot_be_deactivated(migrated_engine, client):
    """Over HTTP the self and root guards make this unreachable, since the
    caller is itself an active admin. It guards the race where two admins
    deactivate each other at once: by the time the second one runs, its caller
    has already gone inactive. Simulated by a caller row already deactivated."""
    caller = _staff(migrated_engine, Role.admin, "goneadmin", active=False)
    last = _staff(migrated_engine, Role.admin, "lastadmin")
    with Session(migrated_engine) as s:
        actor = s.get(User, caller)
        target = s.get(User, last)
        with pytest.raises(users.Refused) as exc:
            users.deactivate(s, actor=actor, target=target)
        assert exc.value.code == "last_admin"


def test_deactivating_an_unknown_user_is_404(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "nadmin")
    res = client.post("/v2/users/999999/deactivate", headers=auth(admin, Role.admin))
    assert res.status_code == 404


# --- Admin reset ---------------------------------------------------------------


def test_reset_forces_a_change_revokes_sessions_and_is_audited(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "resetadmin")
    op = _staff(migrated_engine, Role.operator, "resetop")
    raw = _cookie(_login(client, "resetop", STAFF_PW))

    res = client.post(
        f"/v2/users/{op}/reset-password",
        json={"password": NEW_STAFF_PW},
        headers=auth(admin, Role.admin),
    )
    assert res.status_code == 200, res.text
    assert res.json()["must_change_password"] is True
    [payload] = _payloads(migrated_engine, "user.password_reset")
    assert payload == {"user_id": op, "reset_by": admin}

    client.cookies.clear()
    assert client.post("/v2/auth/refresh", headers=CLIENT, cookies={COOKIE: raw}).status_code == 401
    assert _login(client, "resetop", STAFF_PW).status_code == 401
    assert _login(client, "resetop", NEW_STAFF_PW).status_code == 200


def test_reset_uses_the_targets_rule_so_a_farmer_may_get_eight(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "fresetadmin")
    farmer_id = seed_farmer(migrated_engine, "+254700004002")
    farmer_user = seed_user(migrated_engine, Role.farmer, "fresetfarmer", farmer_id=farmer_id)
    op = _staff(migrated_engine, Role.operator, "fresetop")
    h = auth(admin, Role.admin)

    ok = client.post(
        f"/v2/users/{farmer_user}/reset-password", json={"password": FARMER_PW}, headers=h
    )
    assert ok.status_code == 200, ok.text
    short = client.post(f"/v2/users/{op}/reset-password", json={"password": FARMER_PW}, headers=h)
    assert short.status_code == 422
    assert short.json()["code"] == "password_too_short"
    assert short.json()["details"]["min_length"] == 15


# --- Password policy (D16) -----------------------------------------------------


def test_new_staff_passwords_need_fifteen_characters(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "pwadmin")
    res = client.post(
        "/v2/users", json=_new_user(password="fourteen chars"), headers=auth(admin, Role.admin)
    )
    assert res.status_code == 422
    assert res.json()["code"] == "password_too_short"


def test_farmer_enrolment_needs_eight_characters(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "pwofficer")
    h = auth(officer, Role.field_officer)
    short = client.post(
        "/v2/farmers", json=_enroll_body("+254700004003", password="seven77"), headers=h
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_too_short"
    assert short.json()["details"]["min_length"] == 8
    ok = client.post("/v2/farmers", json=_enroll_body("+254700004003"), headers=h)
    assert ok.status_code == 201, ok.text


def test_login_still_accepts_an_old_short_password(client, migrated_engine):
    seed_user(migrated_engine, Role.operator, "oldshort", password="pw")
    assert _login(client, "oldshort", "pw").status_code == 200


# --- The forced change (D5) ----------------------------------------------------


def test_a_user_who_must_change_is_refused_everywhere_but_me_and_password(client, migrated_engine):
    op = _staff(migrated_engine, Role.operator, "forcedop", must_change=True)
    h = auth(op, Role.operator)
    refused = client.get("/v2/batches", headers=h)
    assert refused.status_code == 403
    assert refused.json()["code"] == "password_change_required"

    me = client.get("/v2/me", headers=h)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True


def test_enrolment_makes_the_farmer_change_the_officers_password(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "enrofficer")
    res = client.post(
        "/v2/farmers", json=_enroll_body("+254700004004"), headers=auth(officer, Role.field_officer)
    )
    assert res.status_code == 201, res.text
    farmer_user = _user(migrated_engine, res.json()["user_id"])
    assert farmer_user.must_change_password is True


def test_changing_the_password_unlocks_the_account(client, migrated_engine):
    op = _staff(migrated_engine, Role.operator, "unlockop", must_change=True)
    login = _login(client, "unlockop", STAFF_PW)
    token = login.json()["access_token"]
    h = {"Authorization": f"Bearer {token}", **CLIENT}

    res = client.post(
        "/v2/auth/password",
        json={"current_password": STAFF_PW, "new_password": NEW_STAFF_PW},
        headers=h,
    )
    assert res.status_code == 200, res.text
    assert res.json()["access_token"]
    assert _cookie(res)  # the caller stays signed in on this device
    assert client.get("/v2/batches", headers=auth(op, Role.operator)).status_code == 200
    assert (
        client.get("/v2/me", headers=auth(op, Role.operator)).json()["must_change_password"]
        is False
    )

    assert _login(client, "unlockop", STAFF_PW).status_code == 401
    assert _login(client, "unlockop", NEW_STAFF_PW).status_code == 200
    [payload] = _payloads(migrated_engine, "user.password_changed")
    assert payload == {"user_id": op}


def test_a_password_change_signs_out_every_other_session(client, migrated_engine):
    _staff(migrated_engine, Role.operator, "sessop")
    other = _cookie(_login(client, "sessop", STAFF_PW))
    mine = _login(client, "sessop", STAFF_PW)
    h = {"Authorization": f"Bearer {mine.json()['access_token']}", **CLIENT}
    res = client.post(
        "/v2/auth/password",
        json={"current_password": STAFF_PW, "new_password": NEW_STAFF_PW},
        headers=h,
        cookies={COOKIE: _cookie(mine)},
    )
    assert res.status_code == 200, res.text
    fresh = _cookie(res)

    client.cookies.clear()
    assert (
        client.post("/v2/auth/refresh", headers=CLIENT, cookies={COOKIE: fresh}).status_code == 200
    )
    client.cookies.clear()
    stale = client.post("/v2/auth/refresh", headers=CLIENT, cookies={COOKIE: other})
    assert stale.status_code == 401


def test_password_change_refusals(client, migrated_engine):
    op = _staff(migrated_engine, Role.operator, "refuseop")
    h = {**auth(op, Role.operator), **CLIENT}

    def change(current: str, new: str, headers=h):
        return client.post(
            "/v2/auth/password",
            json={"current_password": current, "new_password": new},
            headers=headers,
        )

    assert change(STAFF_PW, NEW_STAFF_PW, auth(op, Role.operator)).json()["code"] == (
        "client_header_required"
    )
    wrong = change("not my password at all", NEW_STAFF_PW)
    assert (wrong.status_code, wrong.json()["code"]) == (403, "wrong_password")
    same = change(STAFF_PW, STAFF_PW)
    assert (same.status_code, same.json()["code"]) == (422, "password_unchanged")
    short = change(STAFF_PW, "too short")
    assert (short.status_code, short.json()["code"]) == (422, "password_too_short")
    assert (
        client.post(
            "/v2/auth/password",
            json={"current_password": STAFF_PW, "new_password": NEW_STAFF_PW},
            headers=CLIENT,
        ).status_code
        == 401
    )
    assert _payloads(migrated_engine, "user.password_changed") == []


def test_a_farmer_changes_to_an_eight_character_password(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "f8officer")
    enrolled = client.post(
        "/v2/farmers", json=_enroll_body("+254700004005"), headers=auth(officer, Role.field_officer)
    ).json()
    login = _login(client, "+254700004005", FARMER_PW)
    h = {"Authorization": f"Bearer {login.json()['access_token']}", **CLIENT}
    res = client.post(
        "/v2/auth/password",
        json={"current_password": FARMER_PW, "new_password": "mukurwe9"},
        headers=h,
    )
    assert res.status_code == 200, res.text
    me = client.get("/v2/me", headers=auth(enrolled["user_id"], Role.farmer)).json()
    assert me["must_change_password"] is False


# --- No password reaches a fingerprint or an audit payload (D4) ------------------


def test_enrol_fingerprint_ignores_the_password(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "fpofficer")
    h = {**auth(officer, Role.field_officer), "Idempotency-Key": "enrol-fp-1"}
    first = client.post("/v2/farmers", json=_enroll_body("+254700004006"), headers=h)
    assert first.status_code == 201, first.text
    again = client.post(
        "/v2/farmers", json=_enroll_body("+254700004006", password="another9"), headers=h
    )
    assert again.status_code == 201  # the same key is the same action: replayed
    assert again.json() == first.json()

    with Session(migrated_engine) as s:
        stored = s.execute(
            select(IdempotencyKey.request_fingerprint).where(IdempotencyKey.key == "enrol-fp-1")
        ).scalar_one()
    for pw in (FARMER_PW, "another9"):
        with_pw = hashlib.sha256(canonical_bytes(_enroll_body("+254700004006", password=pw)))
        assert stored != with_pw.hexdigest()


def test_user_create_and_reset_fingerprints_ignore_the_password(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "fpadmin")
    h = {**auth(admin, Role.admin), "Idempotency-Key": "user-fp-1"}
    first = client.post("/v2/users", json=_new_user(username="fpuser"), headers=h)
    assert first.status_code == 201, first.text
    again = client.post(
        "/v2/users", json=_new_user(username="fpuser", password=NEW_STAFF_PW), headers=h
    )
    assert again.status_code == 201 and again.json() == first.json()

    rh = {**auth(admin, Role.admin), "Idempotency-Key": "reset-fp-1"}
    target = first.json()["id"]
    one = client.post(
        f"/v2/users/{target}/reset-password", json={"password": NEW_STAFF_PW}, headers=rh
    )
    two = client.post(f"/v2/users/{target}/reset-password", json={"password": STAFF_PW}, headers=rh)
    assert one.status_code == two.status_code == 200
    assert len(_payloads(migrated_engine, "user.password_reset")) == 1


def test_no_audit_payload_ever_holds_a_password(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "auditadmin", is_root=True)
    h = auth(admin, Role.admin)
    created = client.post("/v2/users", json=_new_user(username="auditop"), headers=h).json()
    client.post(
        f"/v2/users/{created['id']}/reset-password", json={"password": NEW_STAFF_PW}, headers=h
    )
    login = _login(client, "auditop", NEW_STAFF_PW)
    client.post(
        "/v2/auth/password",
        json={"current_password": NEW_STAFF_PW, "new_password": STAFF_PW},
        headers={"Authorization": f"Bearer {login.json()['access_token']}", **CLIENT},
    )
    client.post(f"/v2/users/{created['id']}/deactivate", headers=h)
    officer = seed_user(migrated_engine, Role.field_officer, "auditofficer")
    client.post(
        "/v2/farmers", json=_enroll_body("+254700004007"), headers=auth(officer, Role.field_officer)
    )

    with Session(migrated_engine) as s:
        rows = s.execute(select(AuditLog)).scalars().all()
    assert {r.action for r in rows} >= {
        "user.created",
        "user.password_reset",
        "user.password_changed",
        "user.deactivated",
        "farmer.enrolled",
    }
    for row in rows:
        text = json.dumps(row.payload).lower()
        assert "password" not in text, row.action
        for secret in (STAFF_PW, NEW_STAFF_PW, FARMER_PW):
            assert secret not in json.dumps(row.payload), row.action


# --- Names (D6) ----------------------------------------------------------------


def test_staff_are_named_by_full_name_on_me_and_on_the_batch(client, migrated_engine):
    op = _staff(migrated_engine, Role.operator, "namedop", full_name="Jane Mwende")
    assert client.get("/v2/me", headers=auth(op, Role.operator)).json()["display_name"] == (
        "Jane Mwende"
    )
    batch = create_batch(
        client, migrated_engine, phone="+254700004008", headers=auth(op, Role.operator)
    )
    detail = client.get(f"/v2/batches/{batch['id']}", headers=auth(op, Role.operator)).json()
    assert {s["actor_display_name"] for s in detail["stages"]} == {"Jane Mwende"}


def test_a_staff_member_without_a_name_falls_back_to_the_username(client, migrated_engine):
    op = _staff(migrated_engine, Role.operator, "nonameop")
    assert client.get("/v2/me", headers=auth(op, Role.operator)).json()["display_name"] == (
        "nonameop"
    )


def test_session_rows_are_not_leaked_by_the_user_list(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "leakadmin")
    _login(client, "leakadmin", STAFF_PW)
    body = client.get("/v2/users", headers=auth(admin, Role.admin)).text
    assert "password_hash" not in body and "token" not in body
    with Session(migrated_engine) as s:
        assert s.execute(select(RefreshToken)).first() is not None


def test_farmer_rows_keep_their_profile_link(client, migrated_engine):
    officer = seed_user(migrated_engine, Role.field_officer, "linkofficer")
    res = client.post(
        "/v2/farmers", json=_enroll_body("+254700004009"), headers=auth(officer, Role.field_officer)
    ).json()
    with Session(migrated_engine) as s:
        farmer = s.get(Farmer, res["id"])
        assert farmer is not None and farmer.user_id == res["user_id"]


# --- Contract ------------------------------------------------------------------


def test_new_account_writes_require_the_idempotency_key():
    from app.main import app

    spec = app.openapi()
    for path in (
        "/v2/users",
        "/v2/users/{user_id}/deactivate",
        "/v2/users/{user_id}/reset-password",
    ):
        params = spec["paths"][path]["post"].get("parameters", [])
        key = [p for p in params if p["in"] == "header" and p["name"] == "Idempotency-Key"]
        assert key and key[0]["required"] is True, path


# --- Follow-up: privileged targets (pre-push review) -----------------------------


def test_an_admin_cannot_reset_their_own_password_here(client, migrated_engine):
    """A stolen access token must not let anyone skip the current password."""
    admin = _staff(migrated_engine, Role.admin, "selfreset")
    res = client.post(
        f"/v2/users/{admin}/reset-password",
        json={"password": NEW_STAFF_PW},
        headers=auth(admin, Role.admin),
    )
    assert (res.status_code, res.json()["code"]) == (409, "reset_own_password")


def test_nobody_resets_the_root_admin_over_http(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "rootresetter")
    root = _staff(migrated_engine, Role.admin, "therootadmin", is_root=True)
    res = client.post(
        f"/v2/users/{root}/reset-password",
        json={"password": NEW_STAFF_PW},
        headers=auth(admin, Role.admin),
    )
    assert (res.status_code, res.json()["code"]) == (409, "root_protected")
    assert _login(client, "therootadmin", STAFF_PW).status_code == 200


def test_only_the_root_admin_resets_another_admin(client, migrated_engine):
    admin = _staff(migrated_engine, Role.admin, "plainresetter")
    other = _staff(migrated_engine, Role.admin, "otheradmin2")
    root = _staff(migrated_engine, Role.admin, "rootresetter2", is_root=True)
    body = {"password": NEW_STAFF_PW}
    refused = client.post(
        f"/v2/users/{other}/reset-password", json=body, headers=auth(admin, Role.admin)
    )
    assert (refused.status_code, refused.json()["code"]) == (403, "root_required")
    ok = client.post(f"/v2/users/{other}/reset-password", json=body, headers=auth(root, Role.admin))
    assert ok.status_code == 200, ok.text


def test_a_username_cannot_take_a_phone_login(client, migrated_engine):
    """Enrolment does not normalise phones, so a farmer could have signed in
    as "jmwende"; login then matched two rows."""
    admin = _staff(migrated_engine, Role.admin, "collideadmin")
    farmer_id = seed_farmer(migrated_engine, "+254700004010")
    with Session(migrated_engine) as s:
        s.add(
            User(
                phone="jmwende",
                password_hash=security.hash_password(FARMER_PW),
                role=Role.farmer,
                is_root=False,
                is_active=True,
            )
        )
        s.commit()
    res = client.post("/v2/users", json=_new_user(), headers=auth(admin, Role.admin))
    assert (res.status_code, res.json()["code"]) == (409, "username_taken")
    assert farmer_id


def test_root_recovery_script_service(client, migrated_engine):
    root = _staff(migrated_engine, Role.admin, "lostroot", is_root=True)
    raw = _cookie(_login(client, "lostroot", STAFF_PW))
    with Session(migrated_engine) as s:
        bootstrap.reset_root_password(s, password=NEW_STAFF_PW)
        s.commit()
    assert _login(client, "lostroot", STAFF_PW).status_code == 401
    assert _login(client, "lostroot", NEW_STAFF_PW).status_code == 200
    client.cookies.clear()
    assert client.post("/v2/auth/refresh", headers=CLIENT, cookies={COOKIE: raw}).status_code == 401
    [payload] = _payloads(migrated_engine, "user.password_reset")
    assert payload == {"user_id": root, "reset_by": None}
    with Session(migrated_engine) as s, pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.reset_root_password(s, password="short")
