"""Batch creation (P1-F). The state-changing action of the Phase 1 acceptance
test: creates a batch in CREATED and appends the `batch.created` audit row."""

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import BatchState, Farmer, HoneyBatch, User
from app.routers._context import request_context
from app.schemas.batches import BatchCreateRequest, BatchResponse
from app.services import audit_log, idempotency

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
    response = BatchResponse.model_validate(batch)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response
