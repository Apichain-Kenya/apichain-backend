"""Refresh-token rotation + reuse detection (P1-D, 08 D2)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models import RefreshToken, Role, User
from app.services import refresh_tokens as rt
from app.services import security


def _user(db):
    u = User(username="alice", password_hash="x", role=Role.operator, is_root=False, is_active=True)
    db.add(u)
    db.flush()
    return u


def test_issue_stores_only_the_hash(db):
    user = _user(db)
    raw, row = rt.issue(db, user)
    assert row.token_hash == security.hash_token(raw)
    assert row.token_hash != raw
    assert row.expires_at > datetime.now(UTC).replace(tzinfo=None)


def test_rotate_issues_new_pair_and_marks_old(db):
    user = _user(db)
    raw, old = rt.issue(db, user)
    new_raw, new, grace = rt.rotate(db, raw)
    assert not grace
    assert new_raw != raw
    assert old.rotated_to == new.id
    assert new.token_hash == security.hash_token(new_raw)


def test_reuse_of_rotated_token_revokes_whole_family(db):
    user = _user(db)
    raw, _old = rt.issue(db, user)
    _new_raw, new, _ = rt.rotate(db, raw)  # raw is now spent
    # Outside the reuse window (12 D5) a spent token is theft, not a lost reply.
    new.issued_at = new.issued_at - timedelta(seconds=settings.refresh_reuse_window_seconds + 1)
    db.flush()

    with pytest.raises(rt.ReuseDetected):
        rt.rotate(db, raw)  # replaying the spent token

    # every refresh token for this user is now revoked, including the fresh one
    rows = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
    assert rows and all(r.revoked_at is not None for r in rows)


def test_expired_refresh_is_rejected(db):
    user = _user(db)
    raw, row = rt.issue(db, user)
    row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(rt.InvalidRefreshToken):
        rt.rotate(db, raw)


def test_logout_revokes_the_presented_token(db):
    user = _user(db)
    raw, row = rt.issue(db, user)
    rt.revoke(db, raw)
    assert row.revoked_at is not None


def test_a_spent_token_inside_the_reuse_window_reissues_without_revoking(db):
    """12 D5: the successor was never presented and is seconds old, so the
    old token coming back is a lost response. The unused successor is
    retired, a fresh token issues, and nothing else is revoked."""
    user = _user(db)
    raw, old = rt.issue(db, user)
    _b_raw, b, _ = rt.rotate(db, raw)

    c_raw, c, grace = rt.rotate(db, raw)
    assert grace
    assert b.revoked_at is not None
    assert c.revoked_at is None and c.id != b.id
    assert old.rotated_to == c.id
    assert rt.rotate(db, c_raw).grace is False
