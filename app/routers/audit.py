"""Audit health endpoints (P1-G, P2-E; read from `worker_status` since P5-D).

Liveness fields are unauthenticated so an uptime monitor can watch them; the
divergent-id detail and the worker's own error text stay in the logs and in
`worker_status`. The worker process writes the job results (14 D9), so every
API instance gives the same answer. Before the worker has run (a dev box with
no worker, a fresh deploy) the chain endpoint performs one on-demand check,
so the answer is still real; the anchor endpoint falls back to this
process's own view, as it always did.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WorkerStatus
from app.schemas.anchor import AnchorHealthResponse
from app.schemas.audit import AuditHealthResponse
from app.services import anchoring, integrity
from app.services.worker_status import ANCHOR_STAMP, INTEGRITY

router = APIRouter(prefix="/audit", tags=["audit"])


def _utc(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=UTC) if value else None


@router.get("/health", response_model=AuditHealthResponse)
def audit_health(db: Session = Depends(get_db)) -> AuditHealthResponse:
    row = db.get(WorkerStatus, INTEGRITY)
    detail = row.detail if row is not None else None
    if detail and detail.get("checked_at"):
        return AuditHealthResponse(
            chain_ok=detail["chain_ok"],
            checked_at=_utc(detail["checked_at"]),
            rows_checked=detail["rows_checked"],
        )
    status = integrity.current_status()
    if status.checked_at is None:
        status = integrity.check_with_session(db)
    return AuditHealthResponse(
        chain_ok=status.chain_ok,
        checked_at=status.checked_at,
        rows_checked=status.rows_checked,
    )


@router.get("/anchor-health", response_model=AnchorHealthResponse)
def anchor_health(db: Session = Depends(get_db)) -> AnchorHealthResponse:
    """Anchor lag: how far the public witness trails the log (05 §3.6).

    A separate endpoint rather than extra fields on /audit/health, so the
    Phase 1 contract does not move. `last_error` is only ever the calendar's
    refusal, never an arbitrary exception (which could name internal hosts).
    """
    row = db.get(WorkerStatus, ANCHOR_STAMP)
    detail = row.detail if row is not None and row.detail else None
    if detail is not None:
        anchor_ok = bool(detail.get("anchor_ok", True))
        last_run_at = _utc(detail.get("last_run_at"))
        last_anchor_id = detail.get("last_anchor_id")
        waiting_on = detail.get("waiting_on_audit_id")
        last_error = detail.get("last_error")
    else:
        status = anchoring.current_status()
        anchor_ok = status.anchor_ok
        last_run_at = status.last_run_at
        last_anchor_id = status.last_anchor_id
        waiting_on = status.waiting_on_audit_id
        last_error = status.last_error

    backlog = anchoring.backlog(db)
    return AnchorHealthResponse(
        anchor_ok=anchor_ok,
        last_run_at=last_run_at,
        last_anchor_id=last_anchor_id,
        last_anchored_audit_id=backlog.last_anchored_audit_id,
        waiting_on_audit_id=waiting_on,
        pending_rows=backlog.pending_rows,
        oldest_unanchored_at=(
            backlog.oldest_unanchored_at.replace(tzinfo=UTC)
            if backlog.oldest_unanchored_at is not None
            else None
        ),
        pending_anchors=backlog.pending_anchors,
        last_error=last_error,
    )
