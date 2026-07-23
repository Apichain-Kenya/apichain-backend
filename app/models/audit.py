"""The tamper-evidence spine: `audit_log` and `consent_records` (04 §5.2, §5.5).

`audit_log` is append-only, one row per state-changing action, hash-chained:
`row_hash = keccak256(prev_hash ‖ canonical_bytes(row_without_row_hash))`
(a single keccak — see 08 §1). The writer (P1-C) is the only path that inserts.

`actor_id` is deliberately NOT a foreign key: audit rows must survive a user
deletion so the trail stays intact (04 §5.8 marks users removed, never cascades
the audit). `actor_role` snapshots the role at action time. `subject_type` and
`action` are open strings (grow every phase) validated at the app layer.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import ConsentPurpose, GrantedVia


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_subject", "subject_type", "subject_id"),)

    # BigInteger identity gives a monotonic chain order.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary)  # 32 bytes; genesis zero for row 1
    actor_id: Mapped[int | None] = mapped_column(Integer)  # no FK, by design (audit integrity)
    actor_role: Mapped[str] = mapped_column(String)
    subject_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, index=True)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary)  # keccak256(canonical payload)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)


class ConsentRecord(Base):
    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String)  # 'farmer' | 'user'
    subject_id: Mapped[int] = mapped_column(Integer)
    consent_purpose: Mapped[ConsentPurpose] = mapped_column(
        Enum(ConsentPurpose, name="consent_purpose")
    )
    granted: Mapped[bool] = mapped_column(Boolean)
    granted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    granted_via: Mapped[GrantedVia] = mapped_column(Enum(GrantedVia, name="granted_via"))
    text_version: Mapped[str] = mapped_column(String)
