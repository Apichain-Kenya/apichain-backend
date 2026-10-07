"""Idempotency-Key handling for domain writes (P1-H, 04 §5.4).

`begin()` at the top of a mutating endpoint claims `(key, actor_id)`: a matching
request that already completed replays the stored response (no handler work, so
no second audit row); a same-key/different-body request is a conflict.
`finish()` fills in the response, in the caller's transaction, so it commits
atomically with the domain write and audit row.

Phase 1 policy (08 D6, revised): the header is *honored when present*, not
required — enforcement lands with the offline client in Phase 4, which always
sends one. A request without the header behaves normally.

**Reserve-then-update (P3-A, 10 §4).** Phase 1 made `begin()` a pure read and
`finish()` an INSERT, so two genuinely concurrent same-key requests both passed
`begin()` and the second `finish()` duplicated the primary key — an unhandled
500 that, in every handler here, lands *after* `audit_log.append()` has flushed
and therefore burns an audit id and holes anchor coverage.

`begin()` now inserts the row up front with `ON CONFLICT DO NOTHING`, and
`finish()` UPDATEs it. An UPDATE cannot collide on a primary key, so the failure
mode is gone rather than caught. PostgreSQL makes the losing INSERT *wait* for
the holder's transaction to end and then resolve honestly: if the holder
committed, the conflict stands and we replay its response; if the holder rolled
back, the insert proceeds and the retry is free to do the work. The visible
change is that a same-key request arriving mid-flight now blocks for the
holder's duration instead of racing it — a bounded wait in exchange for a
permanent hole.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.errors import APIError
from app.models import IdempotencyKey
from app.services.canonical import canonical_bytes


@dataclass
class Replay:
    status_code: int
    body: dict[str, Any]


@dataclass
class IdemHandle:
    key: str | None
    actor_id: int
    fingerprint: str | None
    replay: Replay | None


def _fingerprint(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def begin(db: Session, *, key: str | None, actor_id: int, body: dict[str, Any]) -> IdemHandle:
    if key is None:
        return IdemHandle(None, actor_id, None, None)
    fingerprint = _fingerprint(body)

    # RETURNING, not rowcount: with ON CONFLICT DO NOTHING a conflicting insert
    # returns no rows, which is unambiguous, whereas rowcount depends on how the
    # driver and SQLAlchemy chose to execute the statement.
    reserved = db.execute(
        pg_insert(IdempotencyKey)
        .values(key=key, actor_id=actor_id, request_fingerprint=fingerprint)
        .on_conflict_do_nothing(index_elements=["key", "actor_id"])
        .returning(IdempotencyKey.key)
    ).scalar_one_or_none()
    if reserved is not None:
        return IdemHandle(key, actor_id, fingerprint, None)

    # Someone else holds this key and has committed (an aborted holder would
    # have let the insert through). Their row decides what happens next.
    existing = db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.actor_id == actor_id)
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - defensive; the row just conflicted
        raise APIError(409, "idempotency_conflict", "Idempotency-Key is in use")
    if existing.request_fingerprint != fingerprint:
        raise APIError(
            409,
            "idempotency_conflict",
            "Idempotency-Key reused with a different request body",
        )
    if existing.response_code is None or existing.response_body is None:
        # A committed reservation with no response means a handler committed
        # without calling finish(). Refuse rather than replay a half-row.
        raise APIError(
            409,
            "idempotency_conflict",
            "Idempotency-Key belongs to a request that did not complete",
        )
    return IdemHandle(
        key, actor_id, fingerprint, Replay(existing.response_code, existing.response_body)
    )


def finish(db: Session, handle: IdemHandle, *, status_code: int, body: dict[str, Any]) -> None:
    if handle.key is None:
        return
    db.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.key == handle.key,
            IdempotencyKey.actor_id == handle.actor_id,
        )
        .values(response_code=status_code, response_body=body)
    )
