"""`honey_batches` — the operational batch header.

Ported from v1 `app/models/batch.py`, stripped of the on-chain coupling
(04 §5.2): the six `*_tx_hash` columns, the six lifecycle `*_at` columns, and
`blockchain_batch_id` are gone. State lives in a single `state` enum plus
`state_updated_at`; the tamper-evidence record of every transition lives in
`audit_log`, not in per-column timestamps. `batch_code` is the public QR
identifier (assigned in P1-F), replacing the v1 `blockchain_batch_id`.

The per-stage `*_records` tables (harvest, process, ...) land with their
Phase 3 transition endpoints, not here — `CREATED` needs no stage record.
"""

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import BatchState


class HoneyBatch(Base):
    __tablename__ = "honey_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id"))
    state: Mapped[BatchState] = mapped_column(
        Enum(BatchState, name="batch_state"), default=BatchState.CREATED
    )
    state_updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
