"""Contact-verification schemas (P3b-G). Neither response carries the code or
the recipient: the caller already knows who they are verifying, and the code
belongs only on the farmer's phone."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.enums import CommChannel


class VerificationSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: CommChannel


class VerificationSentResponse(BaseModel):
    channel: CommChannel
    expires_at: datetime
    resend_available_at: datetime


class VerificationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: CommChannel
    code: str = Field(pattern=r"^[0-9]{6}$")


class VerificationConfirmedResponse(BaseModel):
    channel: CommChannel
    verified_at: datetime
