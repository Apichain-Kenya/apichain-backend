"""Farmer enrollment schemas (P1-F). The enrollment request carries the consent
grant, which is captured in the same transaction (08 D10)."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FarmerEnrollRequest(BaseModel):
    first_name: str
    last_name: str
    phone: str
    email: str | None = None
    address: str | None = None
    number_of_hives: int | None = None
    # The farmer's own login credential.
    password: str = Field(min_length=1)
    # Consent captured at enrollment (data_processing).
    consent_granted: bool
    consent_text_version: str

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
