from fastapi import APIRouter

from app.schemas.meta import PingResponse

router = APIRouter(tags=["meta"])


@router.get("/ping", response_model=PingResponse)
def ping() -> PingResponse:
    """Typed liveness probe under /v2. Exists to give the contract guardrails
    a real typed endpoint to generate and consume; not a domain endpoint."""
    return PingResponse(service="apichain-backend", version="2.0.0")
