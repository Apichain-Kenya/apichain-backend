"""Milestone notifications, derived from the audit log (P3b-H, 04 §5.7, 11 D8).

**The audit log is the outbox.** The three milestone actions
(`batch.harvest_recorded`, `batch.lab_verified`, `batch.distributed`) are
appended in the same transaction as the transition that caused them. A
notification is therefore derivable from a committed audit row, and the
transition handler does not need to know notifications exist. That is why
`stage_writer.py` is unchanged by this phase: there is nothing in a transition
handler to review for fallibility after the append.

Each tick has two steps, each in its own short transaction:

1. **Enqueue.** Milestone audit rows inside a lookback window that have no
   `communications` row yet. This is an anti-join, not a cursor: two
   transitions commit out of order all the time, and a cursor over audit ids
   would silently skip the one that committed late with the lower id. The
   window keeps a first deployment from texting every farmer about every old
   batch. Every channel gets a row: `queued` if the farmer consented and has a
   reachable contact, `skipped` (no recipient stored) or `failed`
   (`invalid_recipient`) otherwise. So an audit row is considered exactly once,
   and `UNIQUE(source_audit_id, channel)` absorbs a second worker.
2. **Claim and send.** `FOR UPDATE SKIP LOCKED` over `queued` rows and
   `sending` rows whose claim has gone stale, marked `sending` and committed
   **before** any network call, so other replicas see the claim. Then each
   row is dispatched in its own transaction. Consent is checked again at send
   time, because a farmer may withdraw in between, and the send is what the
   consent governs.

**Delivery is at-least-once.** A worker that dies after the provider accepted
a message but before its commit leaves the row in `sending`. After the claim
timeout, another tick sends it again. A duplicate milestone SMS is the price.
A silently lost "your lab results are ready" is the alternative.

Verification codes never come through here (they are sent inline, and the
code is never stored to be re-sent).
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.enums import CommChannel, CommPurpose, CommStatus
from app.models import AuditLog, Communication, Farmer, HoneyBatch
from app.services import comms, comms_templates, consent, phone
from app.services.mailer import EmailSender
from app.services.sms import SmsSender

logger = logging.getLogger("apichain.comms_worker")

# Audit action -> template key. Adding a milestone is one line here and two
# templates in comms_templates.
MILESTONES: dict[str, str] = {
    "batch.harvest_recorded": "milestone.harvest_recorded",
    "batch.lab_verified": "milestone.lab_verified",
    "batch.distributed": "milestone.distributed",
}


@dataclass(frozen=True)
class TickResult:
    enqueued: int
    sent: int
    failed: int
    skipped: int


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _row(
    audit: AuditLog, farmer: Farmer, batch: HoneyBatch, channel: CommChannel, db: Session
) -> dict[str, Any]:
    template = comms_templates.get(MILESTONES[audit.action], channel)
    base = {
        "channel": channel,
        "purpose": CommPurpose.milestone,
        "subject_type": "farmer",
        "subject_id": farmer.id,
        "template_key": template.key,
        "template_version": template.version,
        "locale": template.locale,
        "payload": {"batch_code": batch.batch_code},
        "source_audit_id": audit.id,
        "attempts": 0,
        "created_at": _utcnow(),
    }
    purpose = comms.CONSENT_FOR[(CommPurpose.milestone, channel)]
    if not consent.has_consent(db, subject_type="farmer", subject_id=farmer.id, purpose=purpose):
        # No recipient stored for someone who has not opted in.
        return {**base, "recipient": "", "status": CommStatus.skipped, "error": "no_consent"}

    if channel is CommChannel.sms:
        recipient = phone.to_e164_ke(farmer.phone)
    else:
        recipient = (farmer.email or "").strip() or None
        if recipient is not None and "@" not in recipient:
            recipient = None
    if recipient is None:
        if channel is CommChannel.email and not farmer.email:
            return {**base, "recipient": "", "status": CommStatus.skipped, "error": "no_contact"}
        return {
            **base,
            "recipient": "",
            "status": CommStatus.failed,
            "error": "invalid_recipient",
            "attempts": settings.comms_max_attempts,  # never retried
        }
    return {**base, "recipient": recipient, "status": CommStatus.queued, "error": None}


def enqueue(db: Session) -> int:
    """Derive communications rows from new milestone audit rows."""
    # The audit table's created_at is written by the server's now(), so the
    # window is measured on the same clock, in SQL.
    window = func.now() - dt.timedelta(hours=settings.comms_lookback_hours)
    pending = (
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.action.in_(MILESTONES),
                AuditLog.created_at >= window,
                ~exists().where(Communication.source_audit_id == AuditLog.id),
            )
            .order_by(AuditLog.id)
        )
        .scalars()
        .all()
    )
    rows: list[dict[str, Any]] = []
    for audit in pending:
        batch = db.get(HoneyBatch, int(audit.subject_id))
        farmer = db.get(Farmer, batch.farmer_id) if batch is not None else None
        if batch is None or farmer is None:  # pragma: no cover - FKs make this impossible
            continue
        rows.extend(_row(audit, farmer, batch, channel, db) for channel in CommChannel)
    if not rows:
        return 0
    inserted = db.execute(
        insert(Communication)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_communications_source_channel")
        .returning(Communication.status)
    ).scalars()
    # Rows a concurrent worker already inserted come back as nothing.
    return sum(1 for status in inserted if status is CommStatus.queued)


def claim(db: Session) -> list[int]:
    """Mark a batch of sendable rows `sending` and return their ids."""
    now = _utcnow()
    stale = now - dt.timedelta(seconds=settings.comms_claim_timeout_seconds)
    claimed = (
        db.execute(
            select(Communication)
            .where(
                Communication.purpose == CommPurpose.milestone,
                Communication.attempts < settings.comms_max_attempts,
                or_(
                    Communication.status == CommStatus.queued,
                    and_(
                        Communication.status == CommStatus.sending,
                        Communication.claimed_at < stale,
                    ),
                ),
            )
            .order_by(Communication.id)
            .limit(settings.comms_batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for comm in claimed:
        comm.status = CommStatus.sending
        comm.claimed_at = now
    return [c.id for c in claimed]


def run(
    session_factory: sessionmaker,
    *,
    sms: SmsSender | None = None,
    email: EmailSender | None = None,
) -> TickResult:
    """Scheduler entry point. Owns its sessions and transactions."""
    from app.services import mailer
    from app.services import sms as sms_module

    sms = sms or sms_module.from_settings()
    email = email or mailer.from_settings()

    with session_factory() as db:
        enqueued = enqueue(db)
        db.commit()
    with session_factory() as db:
        ids = claim(db)
        db.commit()  # the claim is visible before any network call

    outcomes = {CommStatus.sent: 0, CommStatus.failed: 0, CommStatus.skipped: 0}
    for comm_id in ids:
        with session_factory() as db:
            comm = db.execute(
                select(Communication).where(Communication.id == comm_id).with_for_update()
            ).scalar_one()
            if comm.status is not CommStatus.sending:  # pragma: no cover - lost a race
                continue
            status = comms.dispatch(
                db, comm, sms=sms, email=email, actor_id=None, actor_role="system"
            )
            if status is CommStatus.failed and comm.attempts < settings.comms_max_attempts:
                comm.status = CommStatus.queued  # retried next tick
            db.commit()
            outcomes[status] = outcomes.get(status, 0) + 1
    if ids:
        logger.info("comms tick: %s claimed, outcomes %s", len(ids), outcomes)
    return TickResult(
        enqueued=enqueued,
        sent=outcomes[CommStatus.sent],
        failed=outcomes[CommStatus.failed],
        skipped=outcomes[CommStatus.skipped],
    )
