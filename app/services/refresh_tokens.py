"""Refresh-token issue / rotate / revoke with reuse detection (P1-D, 08 D2).

Rotation: presenting a valid refresh token issues a new pair and marks the old
row `rotated_to` the new one. Reuse: presenting a token that was already rotated
or revoked is treated as compromise — every refresh token for that user is
revoked (a safe, if blunt, response; per-session families can come later with a
family_id column). Logout revokes only the presented token.

One narrow exception (12 D5): a token rotated out in the last
`refresh_reuse_window_seconds`, whose successor has never been presented, is
treated as a lost response rather than theft. On 2G the server can rotate and
the reply never arrive; without this the farmer is signed out everywhere.
"""

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select, update
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


class Rotation(NamedTuple):
    raw: str
    row: RefreshToken
    # True when this rotation went through the reuse window (12 D5): the
    # caller audits it, because it is the one path where a spent token worked.
    grace: bool


def rotate(db: Session, raw: str) -> Rotation:
    row = _find(db, raw)
    if row is None:
        raise InvalidRefreshToken("unknown refresh token")

    if row.revoked_at is not None:
        _revoke_all_for_user(db, row.user_id)
        raise ReuseDetected("refresh token replay detected")

    grace = False
    if row.rotated_to is not None:
        if not _within_reuse_window(db, row.rotated_to):
            _revoke_all_for_user(db, row.user_id)
            raise ReuseDetected("refresh token replay detected")
        # The phone never received the successor (a lost response), so it
        # presents this token again. Retire the unused successor and issue a
        # fresh one in its place; nobody else can hold it.
        successor = db.get(RefreshToken, row.rotated_to)
        assert successor is not None  # FK guarantees it
        successor.revoked_at = _now()
        grace = True

    if row.expires_at <= _now():
        raise InvalidRefreshToken("refresh token expired")

    user = db.get(User, row.user_id)
    assert user is not None  # FK guarantees it
    new_raw, new_row = issue(db, user)
    row.rotated_to = new_row.id
    db.flush()
    return Rotation(new_raw, new_row, grace)


def _within_reuse_window(db: Session, successor_id: int) -> bool:
    """True if the successor is still unused and was issued within the last
    `refresh_reuse_window_seconds` (12 D5).

    "Unused" means never rotated and never revoked: once the successor itself
    has been presented, the old token coming back cannot be a lost response.
    The age is compared inside Postgres, against the same clock that set
    `issued_at` (its server default), never against Python's clock.
    """
    window = timedelta(seconds=settings.refresh_reuse_window_seconds)
    found = db.execute(
        select(RefreshToken.id).where(
            RefreshToken.id == successor_id,
            RefreshToken.rotated_to.is_(None),
            RefreshToken.revoked_at.is_(None),
            RefreshToken.issued_at >= func.localtimestamp() - window,
        )
    ).scalar_one_or_none()
    return found is not None


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
