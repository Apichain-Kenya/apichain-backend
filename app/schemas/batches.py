"""Batch creation schemas (P1-F)."""

from pydantic import BaseModel, ConfigDict

from app.schemas.common import EntityId, SafeStr


class BatchCreateRequest(BaseModel):
    # Bounded to int32 in P3-D. Unbounded since Phase 1, and `db.get(Farmer, n)`
    # with a larger value is a driver-level numeric overflow, not a miss — the
    # best available explanation for the single unreproduced contract failure
    # the Phase 2 handoff records against this endpoint.
    farmer_id: EntityId
    batch_code: SafeStr | None = None  # server generates a public code if omitted


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_code: str
    farmer_id: int
    state: str
