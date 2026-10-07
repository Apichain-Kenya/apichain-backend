"""Auth request/response schemas (P1-F). Typed throughout — no Union[Typed, dict]
(backend CLAUDE.md principle 2)."""

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import SafeStr


def _within_bcrypt_limit(v: str) -> str:
    if len(v.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return v


class LoginRequest(BaseModel):
    # PostgreSQL `text` cannot store U+0000, so an identifier containing one is
    # rejected at the schema (422) rather than reaching the driver and coming
    # back as a 400 from `data_error_handler`. Login is where this was first
    # reachable; P3-D promoted the inline pattern to the shared `SafeStr` type
    # because Phase 3a adds around ten more free-text fields and the rule stops
    # being one anyone can remember field by field.
    identifier: SafeStr  # phone or username
    password: SafeStr = Field(min_length=1)

    _pw = field_validator("password")(_within_bcrypt_limit)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: SafeStr


class LogoutRequest(BaseModel):
    refresh_token: SafeStr


class StatusResponse(BaseModel):
    status: str
