"""Farmer enrollment schemas (P1-F). The enrollment request carries the consent
grant, which is captured in the same transaction (08 D10)."""

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
