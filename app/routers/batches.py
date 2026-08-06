"""Batch creation (P1-F) and the public anchor-proof view (P2-G).

`POST /v2/batches` is the state-changing action of the Phase 1 acceptance test.
`GET /v2/batches/{id}/anchor-proof` is the consumer-facing half of the trust
model: it hands out everything needed to verify a record's inclusion in the
public anchor offline, and says honestly when a record is not anchored yet.
"""

import base64
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import (
    ApiaryLocation,
    ApiaryRecord,
    BatchMetadata,
    BatchState,
    Farmer,
    HoneyBatch,
    User,
)
from app.routers._context import request_context
from app.schemas.anchor import AnchorProofEntry, AnchorProofResponse, ProofStepOut
from app.schemas.batches import BatchCreateRequest, BatchResponse
from app.services import anchor_proof, audit_log, idempotency, stage_payloads

# honey_batches.id is a 32-bit integer. Declaring the bound on the path
# parameter means an out-of-range id is invalid input (422) instead of
# reaching the driver and coming back as a 400 the schema never promised.
_MAX_INT4 = 2_147_483_647

router = APIRouter(prefix="/batches", tags=["batches"])
_require_create = requires("batch.create")


def _generate_batch_code() -> str:
    return "B-" + uuid.uuid4().hex[:10].upper()


@router.post(
    "",
    response_model=BatchResponse,
    status_code=201,
    responses=error_responses(401, 403, 404, 409),
)
def create_batch(
    body: BatchCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_create),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BatchResponse | JSONResponse:
    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor.id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    farmer = db.get(Farmer, body.farmer_id)
    if farmer is None:
        raise APIError(
            404, "farmer_not_found", "Farmer does not exist", {"farmer_id": body.farmer_id}
        )

    apiary = db.get(ApiaryLocation, body.apiary_id)
    if apiary is None:
        raise APIError(
            404, "apiary_not_found", "Apiary does not exist", {"apiary_id": body.apiary_id}
        )
    if apiary.farmer_id != farmer.id:
        # The provenance claim would otherwise point at someone else's hives.
        raise APIError(
            409,
            "apiary_farmer_mismatch",
            "Apiary belongs to a different farmer",
            {"apiary_id": apiary.id, "farmer_id": farmer.id},
        )

    batch = HoneyBatch(
        farmer_id=farmer.id,
        batch_code=body.batch_code or _generate_batch_code(),
        state=BatchState.CREATED,
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise APIError(409, "batch_code_taken", "batch_code already exists") from exc

    # The two S0 pre-images. `apiary_records` snapshots rather than referencing,
    # so editing the apiary later cannot invalidate this batch's anchored hash
    # (v1 Sprint 6).
    apiary_record = ApiaryRecord(
        batch_id=batch.id,
        apiary_id=apiary.id,
        latitude=apiary.latitude,
        longitude=apiary.longitude,
        altitude=apiary.altitude,
        vegetation_type=apiary.vegetation_type,
        hive_count=apiary.hive_count,
    )
    metadata_record = BatchMetadata(
        batch_id=batch.id,
        honey_type=body.metadata.honey_type,
        expected_yield_kg=body.metadata.expected_yield_kg,
        harvest_window_start=body.metadata.harvest_window_start,
        harvest_window_end=body.metadata.harvest_window_end,
        apiary_management_method=body.metadata.apiary_management_method,
        notes=body.metadata.notes,
    )
    db.add_all([apiary_record, metadata_record])
    db.flush()
    # Server defaults (recorded_at) are assigned by the INSERT, and
    # batch_metadata hashes its own, so read them back before hashing.
    db.refresh(apiary_record)
    db.refresh(metadata_record)

    # Nothing fallible below this line: three appends, then the commit.
    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="batch",
        subject_id=str(batch.id),
        action="batch.created",
        payload={"batch_code": batch.batch_code, "farmer_id": farmer.id, "state": "CREATED"},
        ip=ip,
        user_agent=user_agent,
    )
    # Three rows, not one merged S0 row: each is a distinct fact with its own
    # canonical payload, so /verify can say which of them was altered.
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="batch",
        subject_id=str(batch.id),
        action="batch.apiary_recorded",
        payload=stage_payloads.apiary_record(apiary_record),
        ip=ip,
        user_agent=user_agent,
    )
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="batch",
        subject_id=str(batch.id),
        action="batch.metadata_recorded",
        payload=stage_payloads.batch_metadata(metadata_record),
        ip=ip,
        user_agent=user_agent,
    )
    response = BatchResponse.model_validate(batch)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response


def _as_utc(value: datetime | None) -> datetime | None:
    """Columns are naive UTC (matching audit_log); the wire carries the zone."""
    return value.replace(tzinfo=UTC) if value is not None else None


@router.get(
    "/{batch_id}/anchor-proof",
    response_model=AnchorProofResponse,
    responses=error_responses(404),
)
def batch_anchor_proof(
    batch_id: int = Path(ge=1, le=_MAX_INT4),
    db: Session = Depends(get_db),
) -> AnchorProofResponse:
    """Public: everything needed to verify this batch's records offline.

    Unauthenticated by design — the consumer scanning a jar is not a user
    (04 §5.3). One entry per audit row, each carrying its own status, so a
    record that exists in our log but has no public anchor yet is reported as
    `pending` rather than as a missing field (03 §6).
    """
    batch = db.get(HoneyBatch, batch_id)
    if batch is None:
        raise APIError(404, "batch_not_found", "Batch does not exist", {"batch_id": batch_id})

    entries = anchor_proof.entries_for_subject(db, subject_type="batch", subject_id=str(batch.id))
    return AnchorProofResponse(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        status=anchor_proof.rollup(entries),  # type: ignore[arg-type]
        entries=[
            AnchorProofEntry(
                audit_id=entry.audit_id,
                action=entry.action,
                row_hash=entry.row_hash.hex(),
                status=entry.status,  # type: ignore[arg-type]
                merkle_root=entry.anchor.merkle_root.hex() if entry.anchor else None,
                merkle_path=(
                    [ProofStepOut(sibling=s.sibling.hex(), position=s.position) for s in entry.path]
                    if entry.path is not None
                    else None
                ),
                anchor_target=entry.anchor.anchor_target if entry.anchor else None,
                ots_proof=(
                    base64.b64encode(entry.anchor.anchor_proof).decode() if entry.anchor else None
                ),
                anchored_at=_as_utc(entry.anchor.anchored_at) if entry.anchor else None,
                verified_at=_as_utc(entry.anchor.verified_at) if entry.anchor else None,
            )
            for entry in entries
        ],
    )
