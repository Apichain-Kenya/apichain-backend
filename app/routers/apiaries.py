"""Apiary seeding (P3-E).

An apiary must exist before a batch can claim to come from it, so this is the
first step of the S0 flow. The ownership check below is the one v1 omitted on
`POST /farmers/farm-details/{id}` — JWT and role were checked, the path id was
not, so any farmer's token could mutate any farmer's record. That is still
unpatched in the deployed v1 system; it is not repeated here.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.enums import Role
from app.errors import APIError, error_responses
from app.models import ApiaryLocation, Farmer, User
from app.routers._boundaries import get_geocoder
from app.routers._context import request_context
from app.schemas.apiaries import ApiaryCreateRequest, ApiaryResponse
from app.schemas.common import IdempotencyKeyHeader
from app.services import audit_log, geocode, idempotency, ownership

router = APIRouter(prefix="/apiaries", tags=["apiaries"])
_require_create = requires("apiary.create")
_require_read = requires("apiary.read")


def _farmer_for(db: Session, actor: User, farmer_id: int) -> Farmer:
    farmer = db.get(Farmer, farmer_id)
    if farmer is None:
        raise APIError(404, "farmer_not_found", "Farmer does not exist", {"farmer_id": farmer_id})
    # A farmer may only seed their own sites. Field officers and admins enroll
    # on someone's behalf, so they are not restricted.
    ownership.assert_acts_for_farmer(db, actor, farmer.id)
    return farmer


@router.post(
    "",
    response_model=ApiaryResponse,
    status_code=201,
    responses=error_responses(401, 403, 404),
)
def create_apiary(
    body: ApiaryCreateRequest,
    request: Request,
    idempotency_key: IdempotencyKeyHeader,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_create),
    geocoder: geocode.Geocoder = Depends(get_geocoder),
) -> ApiaryResponse | JSONResponse:
    actor_id = actor.id
    farmer = _farmer_for(db, actor, body.farmer_id)

    # The locality lookup goes out at 2 dp, never the exact point (12 D12).
    # It runs after the ownership check and only for a request that is not a
    # replay, so a refused or replayed request never reaches the third party.
    # The read transaction ends first, so the up-to-3 s call holds no pooled
    # connection (14 D19). It is fail-soft and precedes the audit append, so
    # nothing fallible follows the append.
    locality = None
    if not idempotency.completed(db, key=idempotency_key, actor_id=actor_id):
        db.rollback()
        locality = geocoder(*geocode.locality_point(body.latitude, body.longitude))

    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor_id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)
    # Read again in the write transaction: the rows loaded above were expired
    # when the read transaction ended.
    farmer = _farmer_for(db, actor, body.farmer_id)

    apiary = ApiaryLocation(
        farmer_id=farmer.id,
        name=body.name,
        latitude=body.latitude,
        longitude=body.longitude,
        altitude=body.altitude,
        vegetation_type=body.vegetation_type,
        hive_count=body.hive_count,
        locality=locality,
    )
    db.add(apiary)
    db.flush()

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="apiary",
        subject_id=str(apiary.id),
        action="apiary.created",
        # No name: farmers often name an apiary after themselves, and an
        # audit payload outlives any erasure of the row (14 §0 #9).
        payload={"apiary_id": apiary.id, "farmer_id": farmer.id},
        ip=ip,
        user_agent=user_agent,
    )
    response = ApiaryResponse.model_validate(apiary)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response


@router.get("", response_model=list[ApiaryResponse], responses=error_responses(401, 403, 422))
def list_apiaries(
    farmer_id: int | None = Query(default=None, ge=1, le=2_147_483_647),
    db: Session = Depends(get_db),
    actor: User = Depends(_require_read),
) -> list[ApiaryResponse]:
    """A farmer's apiaries, with exact coordinates (12 §6, D17).

    Only the owning farmer, field officers and admins reach this
    (`apiary.read`), because the exact hive point is sensitive personal data
    (03 §8.1). A farmer defaults to themselves; staff must name the farmer.
    """
    if farmer_id is None:
        farmer_id = ownership.farmer_profile_id(db, actor) if actor.role is Role.farmer else None
        if farmer_id is None:
            raise APIError(422, "farmer_id_required", "Name the farmer whose apiaries to list")
    ownership.assert_acts_for_farmer(db, actor, farmer_id)
    rows = db.execute(
        select(ApiaryLocation)
        .where(ApiaryLocation.farmer_id == farmer_id)
        .order_by(ApiaryLocation.id)
    ).scalars()
    return [ApiaryResponse.model_validate(r) for r in rows]
