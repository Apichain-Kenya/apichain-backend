"""Farmer enrollment (P1-F). The consent-gated protected write of the Phase 1
acceptance test: creates the farmer's credential + profile, captures consent,
and appends the `farmer.enrolled` audit row — all in one transaction."""

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import ConsentPurpose, Farmer, GrantedVia, Role, User
from app.routers._context import request_context
from app.schemas.common import IdempotencyKeyHeader
from app.schemas.consent import (
    ConsentRecordRequest,
    ConsentResponse,
    ConsentStateOut,
    ConsentStateResponse,
)
from app.schemas.farmers import FarmerEnrollRequest, FarmerResponse
from app.services import audit_log, consent, idempotency, ownership, security

router = APIRouter(prefix="/farmers", tags=["farmers"])
_require_enroll = requires("farmer.enroll")
_require_consent_action = requires("farmer.consent")

_FARMER_ID = Path(ge=1, le=2_147_483_647)


@router.post(
    "",
    response_model=FarmerResponse,
    status_code=201,
    responses=error_responses(401, 403, 409),
)
def enroll_farmer(
    body: FarmerEnrollRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_enroll),
    idempotency_key: IdempotencyKeyHeader = None,
) -> FarmerResponse | JSONResponse:
    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor.id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    clash = db.execute(
        select(User.id).where(or_(User.phone == body.phone, User.username == body.phone))
    ).first()
    if (
        clash is not None
        or db.execute(select(Farmer.id).where(Farmer.phone == body.phone)).first() is not None
    ):
        raise APIError(409, "phone_taken", "A user with this phone already exists")

    user = User(
        phone=body.phone,
        password_hash=security.hash_password(body.password),
        role=Role.farmer,
        is_root=False,
        is_active=True,
    )
    db.add(user)
    db.flush()

    farmer = Farmer(
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        email=body.email,
        address=body.address,
        number_of_hives=body.number_of_hives,
        user_id=user.id,
        enrolled_by=actor.id,
    )
    db.add(farmer)
    db.flush()

    # Raises consent_required (422) and rolls the whole enrollment back if the
    # grant is absent — no orphan user/farmer/audit row.
    consent.capture_consent(
        db,
        subject_type="farmer",
        subject_id=farmer.id,
        purpose=ConsentPurpose.data_processing,
        granted=body.consent_granted,
        granted_via=GrantedVia.onboarder,
        text_version=body.consent_text_version,
    )

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="farmer",
        subject_id=str(farmer.id),
        action="farmer.enrolled",
        payload={
            "farmer_id": farmer.id,
            "first_name": body.first_name,
            "last_name": body.last_name,
            "phone": body.phone,
            "enrolled_by": actor.id,
        },
        ip=ip,
        user_agent=user_agent,
    )
    response = FarmerResponse.model_validate(farmer)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response


def _farmer_or_404(db: Session, farmer_id: int) -> Farmer:
    farmer = db.get(Farmer, farmer_id)
    if farmer is None:
        raise APIError(404, "farmer_not_found", "Farmer does not exist", {"farmer_id": farmer_id})
    return farmer


@router.post(
    "/{farmer_id}/consents",
    response_model=ConsentResponse,
    status_code=201,
    responses=error_responses(401, 403, 404, 409, 422),
)
def record_farmer_consent(
    body: ConsentRecordRequest,
    request: Request,
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_consent_action),
    idempotency_key: IdempotencyKeyHeader = None,
) -> ConsentResponse | JSONResponse:
    """Grant or withdraw one consent purpose (03 §7, 11 §5).

    The ledger keeps every choice; the newest row decides. `data_processing`
    cannot be withdrawn here: withdrawing it means "stop processing my data",
    which is the Phase 5 deletion workflow, and recording a withdrawal nothing
    honours would be worse than refusing it.
    """
    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor.id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    farmer = _farmer_or_404(db, farmer_id)
    ownership.assert_acts_for_farmer(db, actor, farmer.id)

    if body.purpose is ConsentPurpose.data_processing and not body.granted:
        raise APIError(
            422,
            "withdrawal_not_supported",
            "data_processing consent is withdrawn through the deletion workflow",
            {"purpose": str(body.purpose)},
        )

    # Derived, never client-supplied (03 §7): who captured it is the point.
    granted_via = GrantedVia.farmer_self if actor.role is Role.farmer else GrantedVia.onboarder
    row = consent.record_consent(
        db,
        subject_type="farmer",
        subject_id=farmer.id,
        purpose=body.purpose,
        granted=body.granted,
        granted_via=granted_via,
        text_version=body.text_version,
    )
    db.refresh(row)

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="farmer",
        subject_id=str(farmer.id),
        action="consent.granted" if body.granted else "consent.withdrawn",
        payload={
            "consent_id": row.id,
            "farmer_id": farmer.id,
            "purpose": str(body.purpose),
            "granted": body.granted,
            "granted_via": str(granted_via),
            "text_version": body.text_version,
        },
        ip=ip,
        user_agent=user_agent,
    )
    response = ConsentResponse(
        id=row.id,
        purpose=row.consent_purpose,
        granted=row.granted,
        granted_via=row.granted_via,
        text_version=row.text_version,
        recorded_at=row.granted_at,
    )
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response


@router.get(
    "/{farmer_id}/consents",
    response_model=ConsentStateResponse,
    responses=error_responses(401, 403, 404),
)
def farmer_consents(
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_consent_action),
) -> ConsentStateResponse:
    """The current state of every purpose. A read, so no audit row (the PII
    `data_access` rows are Phase 5)."""
    farmer = _farmer_or_404(db, farmer_id)
    ownership.assert_acts_for_farmer(db, actor, farmer.id)

    states = []
    for purpose in ConsentPurpose:
        row = consent.current(db, subject_type="farmer", subject_id=farmer.id, purpose=purpose)
        states.append(
            ConsentStateOut(
                purpose=purpose,
                granted=row is not None and row.granted,
                granted_via=row.granted_via if row is not None else None,
                text_version=row.text_version if row is not None else None,
                recorded_at=row.granted_at if row is not None else None,
            )
        )
    return ConsentStateResponse(farmer_id=farmer.id, consents=states)
