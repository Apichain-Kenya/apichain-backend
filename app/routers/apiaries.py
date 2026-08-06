"""Apiary seeding (P3-E).

An apiary must exist before a batch can claim to come from it, so this is the
first step of the S0 flow. The ownership check below is the one v1 omitted on
`POST /farmers/farm-details/{id}` — JWT and role were checked, the path id was
not, so any farmer's token could mutate any farmer's record. That is still
unpatched in the deployed v1 system; it is not repeated here.
"""

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.errors import APIError, error_responses
from app.models import ApiaryLocation, Farmer, User
from app.routers._context import request_context
from app.schemas.apiaries import ApiaryCreateRequest, ApiaryResponse
from app.services import audit_log, idempotency, ownership

router = APIRouter(prefix="/apiaries", tags=["apiaries"])
_require_create = requires("apiary.create")


@router.post(
    "",
    response_model=ApiaryResponse,
    status_code=201,
    responses=error_responses(401, 403, 404),
)
def create_apiary(
    body: ApiaryCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_create),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ApiaryResponse | JSONResponse:
    idem = idempotency.begin(
        db, key=idempotency_key, actor_id=actor.id, body=body.model_dump(mode="json")
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    farmer = db.get(Farmer, body.farmer_id)
    if farmer is None:
        raise APIError(
            404, "farmer_not_found", "Farmer does not exist", {"farmer_id": body.farmer_id}
        )

    # A farmer may only seed their own sites. Field officers and admins enroll
    # on someone's behalf, so they are not restricted.
    ownership.assert_acts_for_farmer(db, actor, farmer.id)

    apiary = ApiaryLocation(
        farmer_id=farmer.id,
        name=body.name,
        latitude=body.latitude,
        longitude=body.longitude,
        altitude=body.altitude,
        vegetation_type=body.vegetation_type,
        hive_count=body.hive_count,
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
        payload={
            "apiary_id": apiary.id,
            "farmer_id": farmer.id,
            "name": apiary.name,
        },
        ip=ip,
        user_agent=user_agent,
    )
    response = ApiaryResponse.model_validate(apiary)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response
