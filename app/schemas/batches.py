"""Batch creation schemas (P1-F)."""

from pydantic import BaseModel, ConfigDict


class BatchCreateRequest(BaseModel):
    farmer_id: int
    batch_code: str | None = None  # server generates a public code if omitted


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_code: str
    farmer_id: int
    state: str
