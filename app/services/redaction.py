"""One redaction for every log line and every Sentry event (P5-C, 14 D10).

Two passes over whatever is about to leave the process:

1. **Keys.** A value under a sensitive key is replaced whole. The key list is
   the columns the models tag as PII (`info={"pii": ...}`, any tag but
   "none"), plus a fixed list of secrets and sign-in fields that no column
   tag covers (`username` is untagged but names a person, 14 P5-A).
2. **Free text.** Phone-shaped and email-shaped substrings are masked in every
   remaining string: a log message, a traceback, a Postgres `DETAIL` line.
   `Key (col)=(value)` fragments, the shape of a unique-violation message,
   lose their value when the column is sensitive.

Coordinates in free text are not recognised; they reach logs only under
their column names, which pass 1 covers.
"""

import re
from collections.abc import Mapping
from functools import cache
from typing import Any

REDACTED = "[redacted]"

_FIXED_KEYS = frozenset(
    {
        "authorization",
        "body",
        "code",
        "cookie",
        "cookies",
        "dsn",
        "identifier",
        "password",
        "set-cookie",
        "token",
        "username",
    }
)
# Any key containing one of these is a secret, whatever its prefix.
_SECRET_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "pepper",
    "session",
    "credential",
    "api_key",
    "apikey",
    "csrf",
)

# Kenyan numbers in every form the app accepts (`phone.to_e164_ke`), and any
# other E.164 number. Letters and digits on either side mean it is part of a
# longer token (a hex id, a timestamp), so it is left alone.
_PHONE = re.compile(r"(?<![0-9A-Za-z])(?:(?:\+?254|0)[17]\d{8}|\+\d{10,15})(?![0-9A-Za-z])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PG_KEY = re.compile(r"Key \((?P<cols>[^)]*)\)=\((?P<vals>[^)]*)\)")


@cache
def sensitive_keys() -> frozenset[str]:
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.database import Base

    tagged = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.info.get("pii", "none") != "none"
    }
    return frozenset(tagged) | _FIXED_KEYS


def is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in sensitive_keys() or any(part in k for part in _SECRET_PARTS)


def _pg_key(match: re.Match[str]) -> str:
    cols = [c.strip() for c in match["cols"].split(",")]
    if any(is_sensitive_key(c) for c in cols):
        return f"Key ({match['cols']})=({REDACTED})"
    return match[0]


def scrub_text(text: str) -> str:
    text = _PG_KEY.sub(_pg_key, text)
    text = _PHONE.sub(REDACTED, text)
    return _EMAIL.sub(REDACTED, text)


def scrub(value: Any) -> Any:
    """A copy of `value` with sensitive keys replaced and free text masked.
    Values that are neither text nor containers pass through unchanged."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            k: REDACTED if isinstance(k, str) and is_sensitive_key(k) else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(scrub(v) for v in value)
    return value
