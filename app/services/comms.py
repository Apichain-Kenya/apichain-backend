"""The one send path (P3b-F, 04 §5.7, 11 §8).

Both callers go through `dispatch`: the verification endpoint (inline, because
a 60-second worker tick is a poor wait for a 10-minute code, and because the
code must never be stored to be re-sent) and the milestone worker.

**Order, and why.**

1. Consent, via a fixed purpose map (11 D2, D7). None: `skipped`, nothing
   sent, no audit row. A skip is not a send.
2. Render from the frozen template. The verification code arrives through
   `secret_vars`, which is merged for rendering and never stored.
3. The provider call: fallible network I/O, so it sits **above** the append.
   A failure marks the row `failed` with a stable code and writes no audit row.
4. Success: the row becomes `sent`, then `audit_log.append(communication.sent)`
   with no recipient and no body (11 D12). Nothing fallible follows; the
   caller commits.

**The window that cannot be closed.** If the provider accepts the message and
the commit afterwards fails, the message has gone and nothing records it. No
provider offers a two-phase send. The window is narrowed to infallible work
plus one commit; for milestones the worker's claim makes the consequence a
possible duplicate, never a silent loss (11 D8).
"""

import datetime as dt
import logging
from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.enums import CommChannel, CommPurpose, CommStatus, ConsentPurpose
from app.models import Communication
from app.services import audit_log, comms_templates, consent
from app.services.mailer import EmailSender
from app.services.sms import SendFailed, SmsSender

logger = logging.getLogger("apichain.comms")

# Which consent governs which send (11 D7). Verification is part of enrolment,
# covered by the mandatory data_processing consent; the notification purposes
# are opt-in and default off (03 §9), so they gate milestones only.
CONSENT_FOR: Mapping[tuple[CommPurpose, CommChannel], ConsentPurpose] = {
    (CommPurpose.verification, CommChannel.sms): ConsentPurpose.data_processing,
    (CommPurpose.verification, CommChannel.email): ConsentPurpose.data_processing,
    (CommPurpose.milestone, CommChannel.sms): ConsentPurpose.sms_notifications,
    (CommPurpose.milestone, CommChannel.email): ConsentPurpose.email_notifications,
}


def dispatch(
    db: Session,
    comm: Communication,
    *,
    sms: SmsSender,
    email: EmailSender,
    actor_id: int | None,
    actor_role: str,
    secret_vars: Mapping[str, str] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> CommStatus:
    """Send one communication and record the outcome. Returns the new status.

    Never raises for a provider failure: the caller decides what a failure
    means (a 503 for a verification request, a retry for the worker).
    """
    if not consent.has_consent(
        db,
        subject_type=comm.subject_type,
        subject_id=comm.subject_id,
        purpose=CONSENT_FOR[(comm.purpose, comm.channel)],
    ):
        comm.status = CommStatus.skipped
        comm.error = "no_consent"
        db.flush()
        return comm.status

    template = comms_templates.get(comm.template_key, comm.channel, comm.locale)
    subject, body = comms_templates.render(template, {**comm.payload, **(secret_vars or {})})

    comm.attempts += 1
    try:
        if comm.channel is CommChannel.sms:
            provider, message_id = sms.provider, sms.send(comm.recipient, body)
        else:
            provider = email.provider
            message_id = email.send(comm.recipient, subject or "", body)
    except SendFailed as exc:
        return _failed(db, comm, exc.code)
    except Exception as exc:  # a provider bug must not escape as a 500
        logger.warning("provider raised %s", type(exc).__name__)
        return _failed(db, comm, "provider_error")

    comm.status = CommStatus.sent
    comm.provider = provider
    comm.provider_message_id = message_id
    comm.error = None
    comm.sent_at = dt.datetime.now(dt.UTC)
    db.flush()

    audit_log.append(
        db,
        actor_id=actor_id,
        actor_role=actor_role,
        subject_type=comm.subject_type,
        subject_id=str(comm.subject_id),
        action="communication.sent",
        payload={
            "communication_id": comm.id,
            "channel": str(comm.channel),
            "purpose": str(comm.purpose),
            "template_key": comm.template_key,
            "template_version": comm.template_version,
            "locale": comm.locale,
            "provider": provider,
            "provider_message_id": message_id,
            "source_audit_id": comm.source_audit_id,
        },
        ip=ip,
        user_agent=user_agent,
    )
    # Nothing fallible below the append; the caller commits.
    return comm.status


def _failed(db: Session, comm: Communication, code: str) -> CommStatus:
    comm.status = CommStatus.failed
    comm.error = code
    db.flush()
    return comm.status
