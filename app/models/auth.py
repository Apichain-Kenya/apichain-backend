"""Auth support tables: `refresh_tokens` (rotation) and `idempotency_keys`.

`refresh_tokens` stores the sha256 HASH of each token, never the token itself.
Rotation (P1-D, 08 D2): issuing a replacement sets `rotated_to` on the old row;
presenting an already-rotated token is reuse → the whole family is revoked.

`idempotency_keys` persists `(key, actor_id) -> response` for a bounded window
so a replayed domain write returns the stored response without re-running the
handler (04 §5.4). Auth endpoints are excluded (08 D6).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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
    response_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
