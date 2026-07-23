"""Audit health schema (P1-G)."""

from datetime import datetime

from pydantic import BaseModel


class AuditHealthResponse(BaseModel):
    chain_ok: bool
    checked_at: datetime | None
    rows_checked: int
