"""Farmer enrollment (P1-F). The consent-gated protected write of the Phase 1
acceptance test: creates the farmer's credential + profile, captures consent,
and appends the `farmer.enrolled` audit row — all in one transaction."""

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import ConsentPurpose, Farmer, GrantedVia, Role, User
from app.routers._context import request_context
from app.schemas.farmers import FarmerEnrollRequest, FarmerResponse
from app.services import audit_log, consent, idempotency, security

router = APIRouter(prefix="/farmers", tags=["farmers"])
_require_enroll = requires("farmer.enroll")


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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
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
