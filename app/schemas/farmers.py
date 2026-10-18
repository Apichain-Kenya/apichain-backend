"""Farmer enrollment schemas (P1-F). The enrollment request carries the consent
grant, which is captured in the same transaction (08 D10)."""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Count, SafeStr


class FarmerEnrollRequest(BaseModel):
    first_name: SafeStr
    last_name: SafeStr
    phone: SafeStr
    email: SafeStr | None = None
    address: SafeStr | None = None
    number_of_hives: Count | None = None
    # The farmer's own login credential.
    password: SafeStr = Field(min_length=1)
    # Consent captured at enrollment (data_processing).
    consent_granted: bool
    consent_text_version: SafeStr

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 bytes")
        return v


class FarmerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str
    last_name: str
    phone: str
    user_id: int | None


# --- Reads (P4b-B, 13 §6, D7) ---------------------------------------------------


class FarmerSummary(BaseModel):
    """One row of `GET /v2/farmers`: field officers and admins only (D7), so
    the phone is in it. No `data_access` audit row yet (Phase 5, 12 D17)."""

    id: int
    display_name: str
    phone: str
    number_of_hives: int | None
    phone_verified: bool
    email_verified: bool
    enrolled_at: dt.datetime
    enrolled_by_display_name: str | None


class FarmerPage(BaseModel):
    items: list[FarmerSummary]
    # Opaque to the client; pass it back as `cursor` for the next page.
    next_cursor: str | None


class FarmerDetail(FarmerSummary):
    """`GET /v2/farmers/{id}`: the profile with its verification state. A
    farmer may read only their own. Consents stay on their own endpoint."""

    first_name: str
    last_name: str
    email: str | None
    address: str | None
    user_id: int | None
    has_email: bool
    phone_verified_at: dt.datetime | None
    email_verified_at: dt.datetime | None
    documents_count: int
    apiaries_count: int
