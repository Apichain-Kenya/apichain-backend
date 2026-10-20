"""Sliding-window rate limits kept in Postgres (P5-B, 14 D7, 04 §5.8).

Each limit counts rows in `rate_limit_events` for one `(scope, key)` inside a
window. The key is a peppered sha256 of what is limited, so the table never
holds a phone, a username or an address.

**The counts commit on their own.** A failed login answers 401, and an
`APIError` rolls the request's transaction back; a failure recorded in that
transaction would never have happened, and the limit would never trip. Every
operation here therefore runs in a short session of its own and commits before
the caller decides anything (the same reason the verification-code attempt
counter commits before raising, `routers/verifications.py`). A per-key
advisory lock makes "count, then add" one step, so concurrent requests cannot
both slip under the limit.

No new service: one table and an index (14 §3, "no new infrastructure"). Rows
for a key are pruned past its window each time that key is touched; rows for
keys never seen again are left for the worker's prune (P5-D).
"""

import datetime as dt
import hashlib
import ipaddress
import logging
import math
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Connection, Engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import APIError
from app.models import RateLimitEvent
from app.services import phone

logger = logging.getLogger("apichain.rate_limit")

Bind = Engine | Connection


@dataclass(frozen=True)
class Limit:
    """A scope and the names of the settings that size it, read at call time
    so a deployment (or a test) can change the numbers."""

    scope: str
    max_setting: str
    window_setting: str

    @property
    def max_events(self) -> int:
        return int(getattr(settings, self.max_setting))

    @property
    def window(self) -> dt.timedelta:
        return dt.timedelta(seconds=int(getattr(settings, self.window_setting)))


LOGIN_PAIR_FAILURES = Limit(
    "login.pair_failures", "login_pair_failures_max", "login_window_seconds"
)
LOGIN_IDENTIFIER_FAILURES = Limit(
    "login.identifier_failures", "login_identifier_failures_max", "login_window_seconds"
)
LOGIN_IP_ATTEMPTS = Limit("login.ip_attempts", "login_ip_attempts_max", "login_window_seconds")
PASSWORD_CHANGE_FAILURES = Limit(
    "password.failures", "password_change_failures_max", "password_change_window_seconds"
)
VERIFICATION_SEND_IP = Limit(
    "verification.ip_sends", "verification_send_ip_max", "ip_hourly_window_seconds"
)
UPLOAD_IP = Limit("upload.ip", "upload_ip_max", "ip_hourly_window_seconds")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def client_ip(request: Request) -> str:
    """The client's address, for limiting.

    The trusted proxy's header when one is configured (App Platform sets
    `do-connecting-ip`), otherwise the socket's peer. Nothing else is read:
    with no trusted header configured, `X-Forwarded-For` and its kin are
    ignored, or a client could choose a fresh address for every guess.
    An address that does not parse counts as "unknown", one shared bucket.
    """
    raw: str | None = None
    if settings.trusted_proxy_header:
        raw = request.headers.get(settings.trusted_proxy_header)
        raw = raw.split(",")[0].strip() if raw else None
    if raw is None and request.client is not None:
        raw = request.client.host
    try:
        return str(ipaddress.ip_address(raw)) if raw else "unknown"
    except ValueError:
        return "unknown"


def identifier_key(identifier: str) -> str:
    """One count per account however its sign-in name is typed: a phone in
    E.164 (14 D18), anything else trimmed and lower-cased."""
    return phone.to_e164_ke(identifier) or identifier.strip().lower()


def pair_key(identifier: str, address: str) -> str:
    """The (identifier, address) key: a separator neither part can contain."""
    return f"{identifier}\x1f{address}"


def _hash(limit: Limit, key: str) -> bytes:
    material = f"{settings.rate_limit_pepper}\x1f{limit.scope}\x1f{key}".encode()
    return hashlib.sha256(material).digest()


def _lock(db: Session, key_hash: bytes) -> None:
    # Advisory locks take a signed bigint; the hash's first eight bytes are as
    # good a spread as any.
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": int.from_bytes(key_hash[:8], "big", signed=True)},
    )


def _window_rows(db: Session, limit: Limit, key_hash: bytes, now: dt.datetime):
    since = now - limit.window
    db.execute(
        delete(RateLimitEvent).where(
            RateLimitEvent.scope == limit.scope,
            RateLimitEvent.key_hash == key_hash,
            RateLimitEvent.at <= since,
        )
    )
    return db.execute(
        select(func.count(), func.min(RateLimitEvent.at)).where(
            RateLimitEvent.scope == limit.scope,
            RateLimitEvent.key_hash == key_hash,
            RateLimitEvent.at > since,
        )
    ).one()


def _refuse(limit: Limit, oldest: dt.datetime, now: dt.datetime) -> APIError:
    wait = max(1, math.ceil((oldest + limit.window - now).total_seconds()))
    logger.warning("rate limited: scope=%s retry_after=%ss", limit.scope, wait)
    return APIError(
        429,
        "rate_limited",
        "Too many attempts; try again later",
        {"retry_after_seconds": wait},
        headers={"Retry-After": str(wait)},
    )


def ensure_allowed(bind: Bind, limit: Limit, key: str) -> None:
    """Raise 429 if `key` is already at its limit. Counts nothing: for limits
    on failures, which are recorded only once a failure has happened."""
    key_hash = _hash(limit, key)
    now = _now()
    with Session(bind) as db:
        _lock(db, key_hash)
        count, oldest = _window_rows(db, limit, key_hash, now)
        db.commit()
    if count >= limit.max_events:
        raise _refuse(limit, oldest, now)


def record(bind: Bind, limit: Limit, key: str) -> None:
    """Count one event (a failure) against `key`."""
    key_hash = _hash(limit, key)
    with Session(bind) as db:
        _lock(db, key_hash)
        db.add(RateLimitEvent(scope=limit.scope, key_hash=key_hash, at=_now()))
        db.commit()


def take(bind: Bind, limit: Limit, key: str) -> None:
    """Count this attempt, or raise 429 if `key` is already at its limit. For
    limits on attempts, where every request counts whatever its outcome."""
    key_hash = _hash(limit, key)
    now = _now()
    with Session(bind) as db:
        _lock(db, key_hash)
        count, oldest = _window_rows(db, limit, key_hash, now)
        if count >= limit.max_events:
            db.commit()
            raise _refuse(limit, oldest, now)
        db.add(RateLimitEvent(scope=limit.scope, key_hash=key_hash, at=now))
        db.commit()


def clear(bind: Bind, limit: Limit, key: str) -> None:
    """Forget every event for `key` (a successful sign-in from that address)."""
    key_hash = _hash(limit, key)
    with Session(bind) as db:
        _lock(db, key_hash)
        db.execute(
            delete(RateLimitEvent).where(
                RateLimitEvent.scope == limit.scope, RateLimitEvent.key_hash == key_hash
            )
        )
        db.commit()
