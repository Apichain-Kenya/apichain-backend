"""Refresh-token issue / rotate / revoke with reuse detection (P1-D, 08 D2).

Rotation: presenting a valid refresh token issues a new pair and marks the old
row `rotated_to` the new one. Reuse: presenting a token that was already rotated
or revoked is treated as compromise — every refresh token for that user is
revoked (a safe, if blunt, response; per-session families can come later with a
family_id column). Logout revokes only the presented token.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RefreshToken, User
from app.services import security


class InvalidRefreshToken(Exception):
    """Token is unknown or expired."""


class ReuseDetected(Exception):
    """A spent (rotated or revoked) token was replayed; the family is revoked."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def issue(db: Session, user: User) -> tuple[str, RefreshToken]:
    raw, token_hash = security.generate_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=_now() + timedelta(days=settings.refresh_token_ttl_days),
    )
    db.add(row)
    db.flush()
    return raw, row


def _find(db: Session, raw: str) -> RefreshToken | None:
    return db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(raw))
    ).scalar_one_or_none()


def rotate(db: Session, raw: str) -> tuple[str, RefreshToken]:
    row = _find(db, raw)
    if row is None:
        raise InvalidRefreshToken("unknown refresh token")

    if row.rotated_to is not None or row.revoked_at is not None:
        _revoke_all_for_user(db, row.user_id)
        raise ReuseDetected("refresh token replay detected")

    if row.expires_at <= _now():
        raise InvalidRefreshToken("refresh token expired")

    user = db.get(User, row.user_id)
    assert user is not None  # FK guarantees it
    new_raw, new_row = issue(db, user)
    row.rotated_to = new_row.id
    db.flush()
    return new_raw, new_row


def revoke(db: Session, raw: str) -> None:
    """Logout: revoke only the presented token."""
    row = _find(db, raw)
    if row is not None and row.revoked_at is None:
        row.revoked_at = _now()
        db.flush()


def _revoke_all_for_user(db: Session, user_id: int) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    db.flush()
