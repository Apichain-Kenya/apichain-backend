"""Security headers on every API response (P5-B, 05 §5.4).

- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on
  everything.
- `Cache-Control: no-store` on every `/v2` route except the public jar
  routes. Those answers carry sessions, personal data and signed URLs that no
  shared cache or browser disk cache should keep. The farmer app's offline
  lists live in its own IndexedDB store (`src/offline/snapshots.ts`), not in
  the HTTP cache, so this cannot regress 4a's offline view. A handler that
  sets its own `Cache-Control` keeps it.
- `Strict-Transport-Security` in production only: on a dev box served over
  plain HTTP it would be ignored at best, and pin a local hostname to HTTPS
  at worst.

The client's Content-Security-Policy is the static site's business (P5-F).
"""

from collections.abc import MutableMapping
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings

HSTS = "max-age=31536000; includeSubDomains"

Message = MutableMapping[str, Any]


def _private(path: str) -> bool:
    return path.startswith("/v2/") and not path.startswith("/v2/public/")


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        private = _private(scope["path"])

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if private:
                    headers.setdefault("Cache-Control", "no-store")
                if settings.app_env == "production":
                    headers.setdefault("Strict-Transport-Security", HSTS)
            await send(message)

        await self.app(scope, receive, with_headers)
