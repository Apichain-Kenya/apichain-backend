"""`GET /health/ops` (P5-D, 14 D8)."""

from pydantic import BaseModel


class OpsHealthResponse(BaseModel):
    """`ok` false means at least one alarm is crossed; `failing` names them
    (see `services/ops_health.py`) and nothing more."""

    ok: bool
    failing: list[str] = []
