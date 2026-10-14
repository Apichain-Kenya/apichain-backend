"""Staff account schemas (P4b-A, 13 §5). No response ever carries a password
or a hash; requests carry a password only on the way in."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import Role
from app.schemas.common import SafeStr
from app.services.users import USERNAME_PATTERN

# A farmer account is only ever made by enrolment (POST /v2/farmers).
StaffRole = Literal["field_officer", "operator", "lab_officer", "admin"]


class UserCreateRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    full_name: SafeStr = Field(min_length=1, max_length=120)
    role: StaffRole
    # The starting password. Length is checked by the shared policy (15 for
    # staff), so the error is the same envelope everywhere.
    password: SafeStr = Field(min_length=1)


class PasswordResetRequest(BaseModel):
    password: SafeStr = Field(min_length=1)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    full_name: str | None
    role: Role
    is_active: bool
    is_root: bool
    must_change_password: bool
    created_at: dt.datetime


class UserPage(BaseModel):
    items: list[UserOut]
    # Opaque to the client; pass it back as `cursor` for the next page.
    next_cursor: str | None
