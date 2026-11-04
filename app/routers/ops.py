"""`GET /health/ops`: the alarm an uptime monitor watches (P5-D, 14 D8).

Unauthenticated, like `/health`, and outside `/v2`. It answers 200 while
every check is green and 503 naming the failing checks otherwise, so a free
external monitor needs nothing but the status code and the body. `/health`
stays the liveness check and never touches the database; this one does.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.ops import OpsHealthResponse
from app.services import ops_health

router = APIRouter(tags=["ops"])


@router.get(
    "/health/ops",
    response_model=OpsHealthResponse,
    responses={503: {"model": OpsHealthResponse, "description": "At least one alarm is red"}},
)
def health_ops(response: Response, db: Session = Depends(get_db)) -> OpsHealthResponse:
    failing = ops_health.failing(db)
    if failing:
        response.status_code = 503
    return OpsHealthResponse(ok=not failing, failing=failing)
