"""Audit health endpoint (P1-G).

Reports the last scheduled integrity result. Liveness fields are unauthenticated
so an uptime monitor can watch `/v2/audit/health`; the divergent-id detail stays
in the logs (admin-accessible). On a fresh process that the scheduler has not
yet run, the first call performs one on-demand check so the answer is real.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.audit import AuditHealthResponse
from app.services import integrity

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
