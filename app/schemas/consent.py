"""Consent endpoint schemas (P3b-B, 11 §5).

`granted_via` is deliberately absent from the request: it is derived from the
actor (a farmer acting for themselves is `farmer_self`, staff are `onboarder`),
because 03 §7's audit property is that a later reader can trust *who*
captured a consent. `extra="forbid"` turns a client attempt to claim it into
a 422 rather than a silently ignored field.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import ConsentPurpose, GrantedVia
from app.schemas.common import SafeStr


class ConsentRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: ConsentPurpose
    granted: bool
    # The version of the consent wording the farmer was shown (03 §7).
    text_version: SafeStr = Field(min_length=1, max_length=64)


class ConsentResponse(BaseModel):
    id: int
    purpose: ConsentPurpose
    granted: bool
    granted_via: GrantedVia
    text_version: str
    recorded_at: datetime


class ConsentStateOut(BaseModel):
    """The current state of one purpose: the newest row, or never recorded."""

    purpose: ConsentPurpose
    granted: bool
    granted_via: GrantedVia | None
    text_version: str | None
    recorded_at: datetime | None


class ConsentStateResponse(BaseModel):
    farmer_id: int
    consents: list[ConsentStateOut]
