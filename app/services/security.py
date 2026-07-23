"""Pure auth crypto (P1-D): bcrypt passwords and JWT tokens. No DB access.

bcrypt directly (not passlib — passlib 1.7.4 breaks on bcrypt 5.x; v2 is a
greenfield DB with no hashes to stay compatible with — 08 D5). bcrypt caps
input at 72 bytes; the request schema (P1-F) enforces that, so callers here pass
already-bounded passwords. Refresh tokens are opaque random strings; only their
sha256 hash is ever stored (08 D2).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(*, sub: int | str, role: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(sub),
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify signature + expiry. Raises jwt.PyJWTError on any fault."""
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def generate_refresh_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). The raw token goes to the client once;
    only the hash is stored."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
