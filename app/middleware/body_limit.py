"""A request-body size limit for the upload route (P3b-E, 04 §5.6).

FastAPI parses a multipart body completely, spooling it to disk, **before**
the handler or any dependency runs. A size check inside the handler therefore
cannot stop a 2 GB upload from being received and written to a temp file; it
can only refuse it afterwards. This middleware refuses it on the way in:

- a declared `Content-Length` over the limit is answered 413 without reading
  a byte of the body;
- a body with no length (chunked) is counted as it streams, and the moment
  the count passes the limit the request is abandoned and answered 413.

The limit is the per-file cap plus room for the multipart envelope and form
fields. The handler still checks the file's exact size; this is the backstop
that bounds what the server will receive at all.
"""

import re
from collections.abc import MutableMapping
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings

# Multipart boundaries, part headers and the doc_type field: generous.
ENVELOPE_BYTES = 64 * 1024

_UPLOAD_PATH = re.compile(r"^/v2/farmers/[^/]+/documents/?$")

Message = MutableMapping[str, Any]


class _TooLarge(Exception):
    pass


def _too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "code": "file_too_large",
            "message": "The upload exceeds the size limit",
            "details": {"max_file_bytes": settings.max_file_bytes},
        },
    )


class UploadSizeLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not _UPLOAD_PATH.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        limit = settings.max_file_bytes + ENVELOPE_BYTES
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _too_large()(scope, receive, send)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if started:  # pragma: no cover - the app never answers mid-parse
                raise
            await _too_large()(scope, receive, send)
