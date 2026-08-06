"""Batch creation schemas (P1-F, extended in P3-E)."""

import datetime as dt
import enum
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.common import EntityId, Measurement, SafeStr


class HoneyType(enum.StrEnum):
    """Validated here rather than as a PostgreSQL enum, so amending the list is
    a one-file edit instead of an `ALTER TYPE` migration (v1 Sprint 8)."""

    acacia = "acacia"
    wildflower = "wildflower"
    eucalyptus = "eucalyptus"
    sunflower = "sunflower"
    mixed = "mixed"


class ApiaryManagementMethod(enum.StrEnum):
    organic = "organic"
    conventional = "conventional"
    regenerative = "regenerative"


class BatchMetadataInput(BaseModel):
    """What the farmer declares about a batch at creation — the S0 pre-image.

    `notes` is carried so `/verify` can show it and is deliberately outside the
    canonical payload, so fixing a typo cannot invalidate anchored history
    (v1 Sprint 8; see `stage_payloads.batch_metadata`).
    """

    honey_type: HoneyType
    expected_yield_kg: Measurement
    harvest_window_start: dt.date
    harvest_window_end: dt.date
    apiary_management_method: ApiaryManagementMethod
    notes: SafeStr | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "BatchMetadataInput":
        if self.harvest_window_end < self.harvest_window_start:
            raise ValueError("harvest_window_end must not precede harvest_window_start")
        return self


class BatchCreateRequest(BaseModel):
    # Bounded to int32 in P3-D. Unbounded since Phase 1, and `db.get(Farmer, n)`
    # with a larger value is a driver-level numeric overflow, not a miss — the
    # best available explanation for the single unreproduced contract failure
    # the Phase 2 handoff records against this endpoint.
    farmer_id: EntityId
    # Both required as of P3-E. There is no untyped fallback and no grace
    # period: v1 shipped `Union[BatchMetadataInput, dict]` for one release and
    # Pydantic's smart union silently routed every typed payload to the dict
    # branch, so metadata was never persisted (Sprint 8 -> 9).
    apiary_id: EntityId
    metadata: BatchMetadataInput
    batch_code: SafeStr | None = None  # server generates a public code if omitted


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_code: str
    farmer_id: int
    state: str


class StageRecordedResponse(BaseModel):
    """What a transition returns: where the batch now is, and what it recorded."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: int
    batch_code: str
    state: str
    audit_id: int
    payload_hash: str


class MetadataPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    honey_type: str
    expected_yield_kg: Decimal
    harvest_window_start: dt.date
    harvest_window_end: dt.date
    apiary_management_method: str
    notes: str | None
