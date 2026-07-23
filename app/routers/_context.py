"""Extract audit context (client IP, user agent) from a request."""

import ipaddress

from fastapi import Request


def request_context(request: Request) -> tuple[str | None, str | None]:
    """Return (ip, user_agent). `ip` is a valid address or None — the audit
    column is INET, and test/proxy hosts (e.g. "testclient", a hostname) are
    not valid addresses, so they normalize to None rather than error."""
    host = request.client.host if request.client else None
    ip: str | None = None
    if host is not None:
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            ip = None
    return ip, request.headers.get("user-agent")
