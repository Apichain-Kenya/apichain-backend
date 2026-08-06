"""The one handler order every lifecycle transition follows (P3-F, 10 §3).

The five transitions differ only in their request shape, stage table, payload
builder and audit action. The order they execute in is identical, and getting
it wrong is expensive in a way tests do not naturally catch, so it lives here
once rather than five times:

    1. reserve the idempotency key      -> replay short-circuit
    2. SELECT ... FOR UPDATE the batch  -> serialize concurrent transitions
    3. 404 if it does not exist
    4. check the transition is legal    -> 409 invalid_transition
    5. insert the stage row and flush   -> 409 stage_already_recorded
    6. advance the batch state
    7. audit_log.append(...)            <-- nothing fallible below this line
    8. finish the idempotency record
    9. the caller commits

**Why step 7 is a line rather than a step.** `append()` flushes its INSERT,
which consumes an `audit_log` id there and then. PostgreSQL sequences are not
transactional, so a rollback afterwards burns that id permanently: the row
never appears, and the anchoring worker can only wait for it, give up, and
record a gap in coverage it can never fill. Every fallible operation therefore
happens above the append, and every refusal returns before it.

**Why both a row lock and a unique constraint.** The `FOR UPDATE` at step 2
serializes two simultaneous transitions on one batch, so the loser sees the
already-advanced state and gets a clean 409. The `UNIQUE(batch_id)` on every
stage table is the backstop that holds even if a future handler forgets the
lock. Lock order is always batch row first, then the audit chain lock that
`append()` takes — never the reverse, which could deadlock.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import BatchState
from app.errors import APIError
from app.models import HoneyBatch, User
from app.routers._context import request_context
from app.schemas.batches import StageRecordedResponse
from app.services import audit_log, idempotency, ownership, transitions


def record_stage(
    db: Session,
    request: Request,
    *,
    actor: User,
    idempotency_key: str | None,
    batch_id: int,
    target: BatchState,
    action: str,
    body: Any,
    build_row: Callable[[int], Any],
    build_payload: Callable[[Any], dict[str, Any]],
    extra: Callable[[Session, Any], dict[str, Any]] | None = None,
) -> StageRecordedResponse | JSONResponse:
    """Record one stage and advance the batch. See the module docstring.

    `extra` runs after the stage row is flushed and before the append, and
    returns additional keys to merge into the audit payload. S3 uses it to fold
    the conformance verdict into the anchored record; it must not be fallible.
    """
    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor.id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    # FOR UPDATE: a second transition on this batch waits here, then sees the
    # advanced state and is refused at step 4 rather than racing to insert.
    batch = db.execute(
        select(HoneyBatch).where(HoneyBatch.id == batch_id).with_for_update()
    ).scalar_one_or_none()
    if batch is None:
        raise APIError(404, "batch_not_found", "Batch does not exist", {"batch_id": batch_id})

    # Role says a farmer may record a harvest; this says whose. Without it any
    # farmer's token could advance any farmer's batch — the same shape as the
    # v1 farm-details IDOR (04 P3). No-op for staff, who are scoped to all
    # batches by design and attributed individually in the audit row.
    ownership.assert_acts_for_farmer(db, actor, batch.farmer_id)

    transitions.assert_transition(batch.state, target)

    row = build_row(batch.id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        # The UNIQUE(batch_id) backstop. Roll back explicitly: once a flush
        # errors, PostgreSQL refuses every further statement in the
        # transaction. This also discards the idempotency reservation, so a
        # retry under the same key gets a fresh 409 rather than a replay —
        # correct, since a refused request should not own the key.
        db.rollback()
        raise APIError(
            409,
            "stage_already_recorded",
            f"This batch already has a {target} record",
            {"batch_id": batch_id, "attempted_state": str(target)},
        ) from exc
    # Server defaults are assigned by the INSERT; read them back before hashing.
    db.refresh(row)

    batch.state = target
    batch.state_updated_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    payload = build_payload(row)
    if extra is not None:
        payload = {**payload, **extra(db, row)}

    ip, user_agent = request_context(request)
    entry = audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="batch",
        subject_id=str(batch.id),
        action=action,
        payload=payload,
        ip=ip,
        user_agent=user_agent,
    )

    response = StageRecordedResponse(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        state=str(batch.state),
        audit_id=entry.id,
        payload_hash=entry.payload_hash.hex(),
    )
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response
