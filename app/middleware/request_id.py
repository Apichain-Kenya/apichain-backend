"""A request id on every log line and every response (P5-C).

A well-formed inbound `X-Request-ID` (8 to 64 letters, digits, `.`, `_` or
`-`) is kept, so an id minted by the proxy follows the request; anything else
is replaced with a fresh one. The id is bound to structlog's context for the
life of the request, so every line logged while handling it carries it, and
it goes back on the response.

This middleware also writes the access line, in place of uvicorn's: method,
path, status and duration. It leaves out the query string, which can carry a
phone (the console's farmer search), and the client's address.

**An unhandled exception.** Starlette turns it into a 500 outside every user
middleware, so that 500 carries no `X-Request-ID`, and uvicorn's traceback
is logged after the id is unbound. The exception is therefore logged here,
with the id, before it is re-raised; and the id is a Sentry tag, so the
event Sentry records for it can be found from the log line.
"""

import logging
import re
import time
import uuid
from collections.abc import MutableMapping
from typing import Any

import sentry_sdk
import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

HEADER = "X-Request-ID"
_WELL_FORMED = re.compile(r"[A-Za-z0-9._-]{8,64}")

logger = logging.getLogger("apichain.request")
access_logger = logging.getLogger("apichain.access")

Message = MutableMapping[str, Any]


def _request_id(scope: Scope) -> str:
    inbound = Headers(scope=scope).get(HEADER)
    if inbound is not None and _WELL_FORMED.fullmatch(inbound):
        return inbound
    return uuid.uuid4().hex


class RequestId:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _request_id(scope)
        status: int | None = None
        started = time.perf_counter()

        async def with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = rid
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=rid):
            sentry_sdk.set_tag("request_id", rid)
            try:
                await self.app(scope, receive, with_id)
            except Exception:
                status = status or 500
                logger.exception("unhandled error")
                raise
            finally:
                access_logger.info(
                    "%s %s %s",
                    scope["method"],
                    scope["path"],
                    status,
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
