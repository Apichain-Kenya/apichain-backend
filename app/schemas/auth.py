"""Auth request/response schemas (P1-F). Typed throughout — no Union[Typed, dict]
(backend CLAUDE.md principle 2)."""

from pydantic import BaseModel, Field, field_validator


def _within_bcrypt_limit(v: str) -> str:
    if len(v.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return v


class LoginRequest(BaseModel):
    identifier: str  # phone or username
    password: str = Field(min_length=1)

    _pw = field_validator("password")(_within_bcrypt_limit)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class StatusResponse(BaseModel):
    status: str
