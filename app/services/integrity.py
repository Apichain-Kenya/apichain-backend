"""Scheduled audit-chain integrity check (P1-G, 04 §5.5).

A background job (wired in app.main lifespan) periodically re-verifies the whole
audit chain and records the result in module state, which `GET /v2/audit/health`
reports. The checker must never write to the chain it is checking — on
divergence it only logs and flips the health flag.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.services.audit_log import verify_chain

logger = logging.getLogger("apichain.integrity")


@dataclass
class IntegrityStatus:
    chain_ok: bool = True
    checked_at: datetime | None = None
    rows_checked: int = 0
    first_divergent_id: int | None = None


_status = IntegrityStatus()


def current_status() -> IntegrityStatus:
    return _status


def reset() -> None:
    """Reset module state (tests)."""
    global _status
    _status = IntegrityStatus()


def check_with_session(db: Session) -> IntegrityStatus:
    verdict = verify_chain(db)
    _status.chain_ok = verdict.ok
    _status.checked_at = datetime.now(UTC)
    _status.rows_checked = verdict.checked
    _status.first_divergent_id = verdict.first_divergent_id
    if not verdict.ok:
        logger.error(
            "audit chain integrity FAILED at id=%s (%s)",
            verdict.first_divergent_id,
            verdict.reason,
        )
    return _status


def run_check(session_factory: sessionmaker) -> IntegrityStatus:
    """Entry point for the scheduler: open a session, verify, update state."""
    with session_factory() as db:
        return check_with_session(db)
