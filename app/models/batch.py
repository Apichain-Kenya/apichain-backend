"""`honey_batches` — the operational batch header.

Ported from v1 `app/models/batch.py`, stripped of the on-chain coupling
(04 §5.2): the six `*_tx_hash` columns, the six lifecycle `*_at` columns, and
`blockchain_batch_id` are gone. State lives in a single `state` enum plus
`state_updated_at`; the tamper-evidence record of every transition lives in
`audit_log`, not in per-column timestamps.

**`public_id` is the jar QR identifier** (P3-I), not `batch_code` and never
`id`. Both public views are keyed by it. `id` is sequential, so a public route
on it lets anyone walk 1..N and scrape every batch; `batch_code` may be
client-supplied and so guessable. `public_id` is 128 random bits from
`secrets`, the v2 counterpart of v1's 64-hex QR id. It is never hashed into
any payload, so it could be rotated without breaking anchored history.

The per-stage `*_records` tables (harvest, process, ...) land with their
Phase 3 transition endpoints, not here — `CREATED` needs no stage record.
"""

import secrets
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import BatchState


def new_public_id() -> str:
    """128 bits from a CSPRNG, as 32 lowercase hex characters."""
    return secrets.token_hex(16)


class HoneyBatch(Base):
    __tablename__ = "honey_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, default=new_public_id
    )
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id"))
    state: Mapped[BatchState] = mapped_column(
        Enum(BatchState, name="batch_state"), default=BatchState.CREATED
    )
    state_updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
