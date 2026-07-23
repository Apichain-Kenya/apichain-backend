"""Idempotency-Key handling for domain writes (P1-H, 04 §5.4).

`begin()` at the top of a mutating endpoint checks for a prior response under
`(key, actor_id)`: a matching request replays the stored response (no handler
work, so no second audit row); a same-key/different-body request is a conflict.
`finish()` persists the response in the caller's transaction, so it commits
atomically with the domain write and audit row.

Phase 1 policy (08 D6, revised): the header is *honored when present*, not
required — enforcement lands with the offline client in Phase 4, which always
sends one. A request without the header behaves normally.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

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
    existing = db.get(IdempotencyKey, (key, actor_id))
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise APIError(
                409,
                "idempotency_conflict",
                "Idempotency-Key reused with a different request body",
            )
        return IdemHandle(
            key, actor_id, fingerprint, Replay(existing.response_code, existing.response_body)
        )
    return IdemHandle(key, actor_id, fingerprint, None)


def finish(db: Session, handle: IdemHandle, *, status_code: int, body: dict[str, Any]) -> None:
    if handle.key is None:
        return
    db.add(
        IdempotencyKey(
            key=handle.key,
            actor_id=handle.actor_id,
            request_fingerprint=handle.fingerprint,
            response_code=status_code,
            response_body=body,
        )
    )
    db.flush()
