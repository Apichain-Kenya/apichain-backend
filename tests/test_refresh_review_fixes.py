"""Pre-merge review fixes to refresh rotation (Phase 4a review, 2026-10-13).

1. The reuse window is one use per spent token. Each grace rotation issued a
   fresh successor, so the 60 s window restarted every time and a stolen token
   replayed every <60 s minted sessions forever.
2. Two refreshes of the same token at once must not both rotate it: the row is
   locked, so the second waits and then takes the grace path, leaving exactly
   one live token in the family.
3. Refresh refuses a deactivated user, as login does.
"""

import threading
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.enums import Role
from app.models import RefreshToken, User
from app.services import refresh_tokens as rt
from tests.helpers import seed_user

COOKIE = "__Secure-apichain_rt"
PWA = {"X-ApiChain-Client": "pwa"}


def _user(db):
    u = User(username="rev", password_hash="x", role=Role.operator, is_root=False, is_active=True)
    db.add(u)
    db.flush()
    return u


def test_a_spent_token_gets_one_grace_use_not_an_endless_one(db):
    user = _user(db)
    raw, _ = rt.issue(db, user)
    rt.rotate(db, raw)  # the reply is lost
    assert rt.rotate(db, raw).grace  # forgiven once

    with pytest.raises(rt.ReuseDetected):  # replayed again: theft, not a lost reply
        rt.rotate(db, raw)
    live = (
        db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    assert live == []


@pytest.fixture
def committed(migrated_engine):
    factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("truncate table refresh_tokens, users restart identity cascade"))


def test_two_concurrent_refreshes_of_one_token_leave_one_live_token(committed, monkeypatch):
    with committed() as s:
        user = _user(s)
        raw, _ = rt.issue(s, user)
        s.commit()
        user_id = user.id

    # Widen the read-then-write window, as a slow 2G retry overlapping the
    # original request does: without a row lock both threads read the token
    # as unrotated and both mint a successor.
    original_issue = rt.issue

    def slow_issue(db, user):
        time.sleep(0.3)
        return original_issue(db, user)

    monkeypatch.setattr(rt, "issue", slow_issue)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        try:
            with committed() as s:
                barrier.wait(timeout=10)
                rt.rotate(s, raw)
                s.commit()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert not errors, errors

    with committed() as s:
        live = (
            s.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.rotated_to.is_(None),
                )
            )
            .scalars()
            .all()
        )
    assert len(live) == 1


def test_refresh_refuses_a_deactivated_user(client, migrated_engine):
    uid = seed_user(migrated_engine, Role.operator, "rev-inactive", password="pw")
    login = client.post("/v2/auth/login", json={"identifier": "rev-inactive", "password": "pw"})
    raw = login.cookies[COOKIE]
    with Session(migrated_engine) as s:
        s.get(User, uid).is_active = False
        s.commit()

    client.cookies.clear()
    res = client.post("/v2/auth/refresh", headers=PWA, cookies={COOKIE: raw})
    assert res.status_code == 401
    assert COOKIE not in res.cookies
