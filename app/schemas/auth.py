"""Auth request/response schemas (P1-F). Typed throughout — no Union[Typed, dict]
(backend CLAUDE.md principle 2)."""

from pydantic import BaseModel, Field, field_validator

from app.enums import Role
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


class PasswordChangeRequest(BaseModel):
    """Change your own password (13 §5 item 9). Length is checked by the
    shared policy for the caller's role, so the error is one envelope."""

    current_password: SafeStr = Field(min_length=1)
    new_password: SafeStr = Field(min_length=1)

    _pw = field_validator("current_password")(_within_bcrypt_limit)


class AccessTokenResponse(BaseModel):
    """Login and refresh return only the short-lived access token. The refresh
    token travels in an HttpOnly cookie and never in a body (12 D3)."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class MeResponse(BaseModel):
    """Who the bearer is (12 §5). `farmer_id` is what a farmer's client needs
    to build its own apiary and batch writes; it is null for staff."""

    user_id: int
    role: Role
    display_name: str
    farmer_id: int | None
    # True until the owner replaces a password someone else chose (13 D5).
    # While it is, every other route answers 403 password_change_required.
    must_change_password: bool


class StatusResponse(BaseModel):
    status: str
