"""`worker_status`: one row per background job, written by the worker process
and read by the health endpoints (P5-D, 14 D9).

Before Phase 5 each process kept its jobs' health in module globals, so with
two API instances each reported its own view. The jobs now run in one worker
process, and what they did lands here, where every API instance reads the
same answer. The row named `worker` carries the heartbeat.

Outside the audit chain: this is operational state, rewritten on every tick.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.types import UtcDateTime


class WorkerStatus(Base):
    __tablename__ = "worker_status"

    job: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # None until a run reaches a verdict; a neutral run leaves it as it was.
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_ok_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Redacted and truncated; never served on a public endpoint.
    last_error: Mapped[str | None] = mapped_column(String(500))
    # True while the job is red, so a Sentry event fires once per transition
    # into red, a restarted worker included.
    alarmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # The job's own result fields (the chain verdict, the anchor gap).
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
