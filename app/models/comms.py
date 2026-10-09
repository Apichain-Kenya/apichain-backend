"""`communications` and `verification_codes` (04 §5.2, §5.7; 11 §6).

**`communications` is the send log and, for milestones, the outbox.** A
milestone row is derived from the audit row that caused it (`source_audit_id`)
by the worker, never written by a transition handler (11 D8).
`UNIQUE(source_audit_id, channel)` makes a second worker, or a second tick,
unable to queue the same notification twice. Verification rows carry no
source audit id; Postgres treats NULLs as distinct, so they never clash.

**What is deliberately not stored.** The rendered message body (it is
reproducible from `template_key` + `template_version` + `locale` + `payload`,
11 D9), and the verification code, which reaches the provider through a
`secret_vars` argument that is never persisted. `payload` holds only the
non-secret template variables.

**`verification_codes` holds an HMAC, never a code.** Six digits is a 10^6
space; an unkeyed hash of it is reversed by enumeration in milliseconds by
anyone with read access to this table. There is no unique constraint: "one
active code per channel" is enforced by superseding under the farmer's row
lock, and the history is what the rate limiter counts (11 D13).
"""

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import CommChannel, CommPurpose, CommStatus
from app.models.types import UtcDateTime


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


_channel = Enum(CommChannel, name="comm_channel")


class Communication(Base):
    __tablename__ = "communications"
    __table_args__ = (
        UniqueConstraint("source_audit_id", "channel", name="uq_communications_source_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[CommChannel] = mapped_column(_channel)
    purpose: Mapped[CommPurpose] = mapped_column(Enum(CommPurpose, name="comm_purpose"))
    subject_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[int] = mapped_column(Integer)
    # Normalized E.164 or an email address.
    recipient: Mapped[str] = mapped_column(String, info={"pii": "contact"})
    template_key: Mapped[str] = mapped_column(String)
    template_version: Mapped[int] = mapped_column(Integer)
    locale: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The milestone's cause. BigInteger to match audit_log.id; not an FK, so
    # the log is never coupled to audit-row lifecycles.
    source_audit_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[CommStatus] = mapped_column(Enum(CommStatus, name="comm_status"))
    provider: Mapped[str | None] = mapped_column(String)
    provider_message_id: Mapped[str | None] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # A stable short code, never a provider stack trace.
    error: Mapped[str | None] = mapped_column(String)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=_utcnow)


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id"), index=True)
    channel: Mapped[CommChannel] = mapped_column(_channel)
    code_hmac: Mapped[bytes] = mapped_column(LargeBinary)
    communication_id: Mapped[int | None] = mapped_column(ForeignKey("communications.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=_utcnow)
