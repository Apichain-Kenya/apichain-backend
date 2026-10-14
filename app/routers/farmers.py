"""Farmer enrollment (P1-F). The consent-gated protected write of the Phase 1
acceptance test: creates the farmer's credential + profile, captures consent,
and appends the `farmer.enrolled` audit row — all in one transaction."""

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import ApiaryLocation, ConsentPurpose, Document, Farmer, GrantedVia, Role, User
from app.routers._context import request_context
from app.routers._search import ESCAPE, like_escape
from app.schemas.common import IdempotencyKeyHeader
from app.schemas.consent import (
    ConsentRecordRequest,
    ConsentResponse,
    ConsentStateOut,
    ConsentStateResponse,
)
from app.schemas.farmers import (
    FarmerDetail,
    FarmerEnrollRequest,
    FarmerPage,
    FarmerResponse,
    FarmerSummary,
)
from app.services import (
    audit_log,
    consent,
    idempotency,
    identity,
    ownership,
    password_policy,
    security,
)

router = APIRouter(prefix="/farmers", tags=["farmers"])
_require_enroll = requires("farmer.enroll")
_require_consent_action = requires("farmer.consent")
_require_read = requires("farmer.read")

_FARMER_ID = Path(ge=1, le=2_147_483_647)


@router.post(
    "",
    response_model=FarmerResponse,
    status_code=201,
    responses=error_responses(401, 403, 409, 422),
)
def enroll_farmer(
    body: FarmerEnrollRequest,
    request: Request,
    idempotency_key: IdempotencyKeyHeader,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_enroll),
) -> FarmerResponse | JSONResponse:
    # The password never reaches the fingerprint (13 D4): it is a fast,
    # unsalted hash kept forever beside fields anyone holding it knows.
    idem = idempotency.begin(
        db,
        key=idempotency_key,
        actor_id=actor.id,
        body=body.model_dump(mode="json"),
        exclude=frozenset({"password"}),
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)
    password_policy.check(Role.farmer, body.password)

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
        # The officer chose it, so the farmer must choose their own at their
        # first sign-in (13 D5).
        must_change_password=True,
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
    idempotency_key: IdempotencyKeyHeader,
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_consent_action),
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

    # A data subject's own withdrawal is reversible only by the data subject.
    # Otherwise an officer re-granting sms_notifications silently undoes a
    # farmer's "stop texting me", and the ledger shows consent the farmer
    # explicitly took back. The question is the farmer's own latest choice,
    # not the newest row: staff recording a withdrawal first must not turn the
    # farmer's withdrawal into one staff may then reverse.
    if body.granted and granted_via is GrantedVia.onboarder:
        own = consent.current(
            db,
            subject_type="farmer",
            subject_id=farmer.id,
            purpose=body.purpose,
            granted_via=GrantedVia.farmer_self,
        )
        if own is not None and not own.granted:
            raise APIError(
                409,
                "withdrawn_by_farmer",
                "The farmer withdrew this consent themselves; only they can grant it again",
                {"purpose": str(body.purpose)},
            )

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


# --- Reads (P4b-B, 13 §6, D7) ---------------------------------------------------


def _summaries(db: Session, farmers: list[Farmer]) -> list[FarmerSummary]:
    officers = identity.display_names(db, {f.enrolled_by for f in farmers if f.enrolled_by})
    return [
        FarmerSummary(
            id=f.id,
            display_name=identity.farmer_name(f),
            phone=f.phone,
            number_of_hives=f.number_of_hives,
            phone_verified=f.phone_verified_at is not None,
            email_verified=f.email_verified_at is not None,
            enrolled_at=f.created_at,
            enrolled_by_display_name=officers.get(f.enrolled_by) if f.enrolled_by else None,
        )
        for f in farmers
    ]


@router.get("", response_model=FarmerPage, responses=error_responses(401, 403))
def list_farmers(
    q: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, pattern=r"^[1-9][0-9]{0,9}$"),
    db: Session = Depends(get_db),
    actor: User = Depends(_require_read),
) -> FarmerPage:
    """Every farmer, newest first, for field officers and admins (D7): an
    officer covering a colleague's area must find that colleague's farmers.
    `q` matches the start of a first name, a last name or the full name, or
    the end of a phone number, ignoring case."""
    if actor.role is Role.farmer:
        raise APIError(403, "forbidden", "Farmers cannot list farmers")

    query = select(Farmer).order_by(Farmer.id.desc()).limit(limit + 1)
    if q is not None:
        term = like_escape(q.strip())
        full_name = Farmer.first_name + " " + Farmer.last_name
        query = query.where(
            or_(
                Farmer.first_name.ilike(f"{term}%", escape=ESCAPE),
                Farmer.last_name.ilike(f"{term}%", escape=ESCAPE),
                full_name.ilike(f"{term}%", escape=ESCAPE),
                Farmer.phone.like(f"%{term}", escape=ESCAPE),
            )
        )
    if cursor is not None:
        query = query.where(Farmer.id < int(cursor))
    rows = list(db.execute(query).scalars())
    page = rows[:limit]
    return FarmerPage(
        items=_summaries(db, page),
        next_cursor=str(page[-1].id) if len(rows) > limit else None,
    )


@router.get("/{farmer_id}", response_model=FarmerDetail, responses=error_responses(401, 403, 404))
def read_farmer(
    farmer_id: int = _FARMER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_read),
) -> FarmerDetail:
    """A farmer's profile. A farmer reads only their own (ownership check);
    operators and lab officers never reach it (D7)."""
    farmer = _farmer_or_404(db, farmer_id)
    ownership.assert_acts_for_farmer(db, actor, farmer.id)

    documents = db.execute(
        select(func.count(Document.id)).where(
            Document.subject_type == "farmer", Document.subject_id == farmer.id
        )
    ).scalar_one()
    apiaries = db.execute(
        select(func.count(ApiaryLocation.id)).where(ApiaryLocation.farmer_id == farmer.id)
    ).scalar_one()
    [summary] = _summaries(db, [farmer])
    return FarmerDetail(
        **summary.model_dump(),
        first_name=farmer.first_name,
        last_name=farmer.last_name,
        email=farmer.email,
        address=farmer.address,
        user_id=farmer.user_id,
        has_email=farmer.email is not None,
        phone_verified_at=farmer.phone_verified_at,
        email_verified_at=farmer.email_verified_at,
        documents_count=documents,
        apiaries_count=apiaries,
    )
