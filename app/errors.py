"""Standard error envelope (04 §5.4): {code, message, details?} with stable
`code` values the frontend maps to localized strings.
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """OpenAPI shape of the error envelope, so documented error responses match
    what the handler emits (keeps the Schemathesis contract test honest)."""

    code: str
    message: str
    details: dict[str, Any] | None = None


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Build a FastAPI `responses=` map documenting the error envelope for each
    given status code. Always documents 400 (Starlette returns it with a plain
    {detail} body for an unparseable request body) as description-only, so its
    shape is not validated against the envelope."""
    responses: dict[int | str, dict[str, Any]] = {400: {"description": "Malformed request body"}}
    for code in status_codes:
        responses[code] = {"model": ErrorResponse}
    return responses


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)
