"""Audit health endpoint (P1-G).

Reports the last scheduled integrity result. Liveness fields are unauthenticated
so an uptime monitor can watch `/v2/audit/health`; the divergent-id detail stays
in the logs (admin-accessible). On a fresh process that the scheduler has not
yet run, the first call performs one on-demand check so the answer is real.
"""

from datetime import UTC

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, MerkleAnchor
from app.schemas.anchor import AnchorHealthResponse
from app.schemas.audit import AuditHealthResponse
from app.services import anchoring, integrity

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/health", response_model=AuditHealthResponse)
def audit_health(db: Session = Depends(get_db)) -> AuditHealthResponse:
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
    Phase 1 contract does not move. Phase 5's alarms read these fields.
    """
    status = anchoring.current_status()
    last_anchored = db.execute(select(func.max(MerkleAnchor.to_audit_id))).scalar_one_or_none()

    # Audit ids are positive, so "everything" is simply "> 0" when nothing has
    # been anchored yet — no branch, and no subquery to accidentally cross-join.
    unanchored_from = last_anchored if last_anchored is not None else 0
    pending_rows = db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.id > unanchored_from)
    ).scalar_one()
    oldest_unanchored = db.execute(
        select(func.min(AuditLog.created_at)).where(AuditLog.id > unanchored_from)
    ).scalar_one_or_none()
    pending_anchors = db.execute(
        select(func.count()).select_from(MerkleAnchor).where(MerkleAnchor.verified_at.is_(None))
    ).scalar_one()

    return AnchorHealthResponse(
        anchor_ok=status.anchor_ok,
        last_run_at=status.last_run_at,
        last_anchor_id=status.last_anchor_id,
        last_anchored_audit_id=last_anchored,
        pending_rows=pending_rows,
        oldest_unanchored_at=(
            oldest_unanchored.replace(tzinfo=UTC) if oldest_unanchored is not None else None
        ),
        pending_anchors=pending_anchors,
        last_error=status.last_error,
    )
