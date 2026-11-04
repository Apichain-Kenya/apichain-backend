"""Auth support tables: `refresh_tokens` (rotation), `idempotency_keys` and
`rate_limit_events`.

`refresh_tokens` stores the sha256 HASH of each token, never the token itself.
Rotation (P1-D, 08 D2): issuing a replacement sets `rotated_to` on the old row;
presenting an already-rotated token is reuse → the whole family is revoked.

`idempotency_keys` persists `(key, actor_id) -> response` for a bounded window
so a replayed domain write returns the stored response without re-running the
handler (04 §5.4). Auth endpoints are excluded (08 D6).

`rate_limit_events` is one row per counted event (a failed login, a code
send), keyed by a peppered hash of what is being limited, so it holds no
phone, username or address (P5-B, 14 D7). It is outside the audit chain on
purpose: a refusal written to the chain would let anyone write to it.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.types import UtcDateTime


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    rotated_to: Mapped[int | None] = mapped_column(ForeignKey("refresh_tokens.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    actor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String)
    # Nullable because the row is inserted as a *reservation* when the request
    # starts and filled in when it finishes (P3-A). A committed row always has
    # both; a reservation whose handler failed rolls back with it.
    response_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RateLimitEvent(Base):
    __tablename__ = "rate_limit_events"
    __table_args__ = (Index("ix_rate_limit_events_scope_key_at", "scope", "key_hash", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope: Mapped[str] = mapped_column(String(40))
    key_hash: Mapped[bytes] = mapped_column(LargeBinary)  # 32 bytes
    at: Mapped[datetime] = mapped_column(UtcDateTime)
