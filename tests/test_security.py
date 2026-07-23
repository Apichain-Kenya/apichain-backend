"""Pure-crypto auth primitives (P1-D): password hashing + JWT. No DB."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.services import security


def test_password_round_trip():
    h = security.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert security.verify_password("correct horse battery staple", h)
    assert not security.verify_password("wrong", h)


def test_access_token_encodes_sub_role_and_type():
    token = security.create_access_token(sub=42, role="operator")
    claims = security.decode_token(token)
    assert claims["sub"] == "42"
    assert claims["role"] == "operator"
    assert claims["type"] == "access"


def test_expired_access_token_is_rejected():
    # Forge an already-expired token with the app secret.
    past = datetime.now(UTC) - timedelta(minutes=1)
    token = jwt.encode(
        {"sub": "1", "role": "admin", "type": "access", "exp": past},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_token(token)


def test_token_signed_with_wrong_secret_is_rejected():
    token = jwt.encode({"sub": "1", "type": "access"}, "not-the-secret", algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_token(token)


def test_refresh_token_generation_and_hash_are_stable():
    raw, token_hash = security.generate_refresh_token()
    assert raw and token_hash
    assert security.hash_token(raw) == token_hash
    assert security.hash_token(raw) != raw  # stored value is the hash, not the token
