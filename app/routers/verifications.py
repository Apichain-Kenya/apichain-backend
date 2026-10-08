"""Enrolment contact verification: send a code, confirm it (P3b-G, 11 §9).

**Send order.** The provider call sits above the append, as everywhere:

    1. farmer, FOR UPDATE (serializes sends for this farmer) -> 404
    2. ownership                                             -> 403
    3. data_processing consent (11 D7)                       -> 422 consent_required
    4. recipient: Kenyan E.164 / the email on file           -> 422 invalid_recipient
    5. rate limit (11 D13)                                   -> 429 + Retry-After
    6. supersede the active code; insert code (HMAC) + communications row
    7. comms.dispatch: provider call, then communication.sent append
         on failure: supersede the new code, COMMIT the failed row, then 503
    8. commit; 202 (never the code, never the recipient)

**Confirm order, and the one property that matters.** A wrong code increments
the attempt counter and **commits before raising**. An `APIError` rolls the
transaction back, so an increment followed by a raise would never have
happened, and an attacker would get unlimited guesses at a one-in-a-million
code with every response looking correct.

The farmer row lock in step 1 is held across the provider call. A slow
provider therefore stalls only this farmer's concurrent verification
requests, not other farmers and not the audit chain (the append comes after
the send). The adapters carry hard timeouts.
"""

import datetime as dt

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.enums import CommChannel, CommPurpose, CommStatus, ConsentPurpose
from app.errors import APIError, error_responses
from app.models import Communication, Farmer, User, VerificationCode
from app.routers._boundaries import get_email, get_sms
from app.routers._context import request_context
from app.schemas.verifications import (
    VerificationConfirmedResponse,
    VerificationConfirmRequest,
    VerificationSendRequest,
    VerificationSentResponse,
)
from app.services import audit_log, comms, consent, ownership, phone
from app.services import verification_codes as codes
from app.services.mailer import EmailSender
from app.services.sms import SmsSender

router = APIRouter(prefix="/farmers", tags=["verifications"])
_require_send = requires("farmer.verify_contact")
_require_confirm = requires("farmer.confirm_contact")

_FARMER_ID = Path(ge=1, le=2_147_483_647)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC)


def _recipient(farmer: Farmer, channel: CommChannel) -> str | None:
    if channel is CommChannel.sms:
        return phone.to_e164_ke(farmer.phone)
    email = (farmer.email or "").strip()
    return email if "@" in email else None


@router.post(
    "/{farmer_id}/verifications",
    response_model=VerificationSentResponse,
    status_code=202,
    responses=error_responses(401, 403, 404, 422, 429, 503),
)
def send_verification_code(
    body: VerificationSendRequest,
    request: Request,
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_send),
    sms: SmsSender = Depends(get_sms),
    email: EmailSender = Depends(get_email),
) -> VerificationSentResponse:
    farmer = db.execute(
        select(Farmer).where(Farmer.id == farmer_id).with_for_update()
    ).scalar_one_or_none()
    if farmer is None:
        raise APIError(404, "farmer_not_found", "Farmer does not exist", {"farmer_id": farmer_id})
    ownership.assert_acts_for_farmer(db, actor, farmer.id)
    consent.require_consent(
        db, subject_type="farmer", subject_id=farmer.id, purpose=ConsentPurpose.data_processing
    )

    channel = body.channel
    recipient = _recipient(farmer, channel)
    if recipient is None:
        raise APIError(
            422,
            "invalid_recipient",
            "There is no reachable contact on file for this channel",
            {"channel": str(channel)},
        )

    now = codes.utcnow()
    recent = (
        db.execute(
            select(VerificationCode.created_at)
            .where(
                VerificationCode.farmer_id == farmer.id,
                VerificationCode.channel == channel,
                VerificationCode.created_at > now - codes.WINDOW,
            )
            .order_by(VerificationCode.id.desc())
        )
        .scalars()
        .all()
    )
    wait = codes.retry_after(recent, now)
    if wait is not None:
        raise APIError(
            429,
            "rate_limited",
            "Too many verification codes requested; try again later",
            {"retry_after_seconds": wait},
            headers={"Retry-After": str(wait)},
        )

    db.execute(
        update(VerificationCode)
        .where(
            VerificationCode.farmer_id == farmer.id,
            VerificationCode.channel == channel,
            VerificationCode.consumed_at.is_(None),
            VerificationCode.superseded_at.is_(None),
        )
        .values(superseded_at=now)
    )

    code = codes.generate()
    comm = Communication(
        channel=channel,
        purpose=CommPurpose.verification,
        subject_type="farmer",
        subject_id=farmer.id,
        recipient=recipient,
        template_key="verification_code",
        template_version=1,
        locale="en",
        # The code is not here: it reaches the provider via secret_vars only.
        payload={"minutes": str(int(codes.CODE_TTL.total_seconds() // 60))},
        status=CommStatus.sending,
        claimed_at=now,
    )
    db.add(comm)
    db.flush()
    issued = VerificationCode(
        farmer_id=farmer.id,
        channel=channel,
        code_hmac=codes.mac(farmer.id, channel, code),
        communication_id=comm.id,
        expires_at=now + codes.CODE_TTL,
        created_at=now,
    )
    db.add(issued)
    db.flush()

    ip, user_agent = request_context(request)
    status = comms.dispatch(
        db,
        comm,
        sms=sms,
        email=email,
        actor_id=actor.id,
        actor_role=actor.role,
        secret_vars={"code": code},
        ip=ip,
        user_agent=user_agent,
    )
    if status is not CommStatus.sent:
        # Keep the failed row (it is a fact, and it counts toward the limit),
        # retire the code nobody received, then refuse.
        issued.superseded_at = now
        db.commit()
        raise APIError(503, "send_failed", "The code could not be sent; try again shortly")

    db.commit()
    return VerificationSentResponse(
        channel=channel,
        expires_at=_as_utc(issued.expires_at),
        resend_available_at=_as_utc(now + codes.COOLDOWN),
    )


@router.post(
    "/{farmer_id}/verifications/confirm",
    response_model=VerificationConfirmedResponse,
    responses=error_responses(401, 403, 404, 422),
)
def confirm_verification_code(
    body: VerificationConfirmRequest,
    request: Request,
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_confirm),
) -> VerificationConfirmedResponse | JSONResponse:
    farmer = db.get(Farmer, farmer_id)
    if farmer is None:
        raise APIError(404, "farmer_not_found", "Farmer does not exist", {"farmer_id": farmer_id})
    ownership.assert_acts_for_farmer(db, actor, farmer.id)

    channel = body.channel
    now = codes.utcnow()
    active = db.execute(
        select(VerificationCode)
        .where(
            VerificationCode.farmer_id == farmer.id,
            VerificationCode.channel == channel,
            VerificationCode.consumed_at.is_(None),
            VerificationCode.superseded_at.is_(None),
        )
        .order_by(VerificationCode.id.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    # Absent, superseded, consumed and expired all answer the same way, so the
    # response says nothing about which.
    if active is None or now >= active.expires_at:
        raise APIError(422, "no_active_code", "There is no active code; request a new one")
    if active.attempts >= codes.MAX_ATTEMPTS:
        raise APIError(422, "code_locked", "Too many wrong attempts; request a new code")

    if not codes.matches(active.code_hmac, farmer.id, channel, body.code):
        active.attempts += 1
        remaining = codes.MAX_ATTEMPTS - active.attempts
        # Commit BEFORE raising: the raise rolls back anything uncommitted,
        # and an uncounted wrong guess is an unlimited one.
        db.commit()
        raise APIError(
            422, "invalid_code", "That code is not correct", {"attempts_remaining": remaining}
        )

    active.consumed_at = now
    if channel is CommChannel.sms:
        farmer.phone_verified_at = now
    else:
        farmer.email_verified_at = now
    db.flush()

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="farmer",
        subject_id=str(farmer.id),
        action="farmer.contact_verified",
        # No phone, no email, no code (11 D12).
        payload={"farmer_id": farmer.id, "channel": str(channel), "verification_id": active.id},
        ip=ip,
        user_agent=user_agent,
    )
    db.commit()
    return VerificationConfirmedResponse(channel=channel, verified_at=_as_utc(now))
