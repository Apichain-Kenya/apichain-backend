from pydantic import BaseModel


class PingResponse(BaseModel):
    """Minimal typed response so the OpenAPI schema has real content for the
    generated TypeScript client to consume. Phase 1 replaces this with the
    real domain schemas."""

    service: str
    version: str
