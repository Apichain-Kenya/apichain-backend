"""Column types that enforce a storage invariant rather than trusting callers.

`UtcDateTime` exists because of a hazard the v1 Sprint 6 watchlist half-caught.
That note says every `DateTime` entering a canonical payload must go through
`canonical_dt`, which normalizes a tz-aware value to naive UTC *at hash time*.
It does — but by then the database has already stored something else.

The lifecycle columns are `TIMESTAMP WITHOUT TIME ZONE`. Handed a tz-aware
datetime, PostgreSQL converts it to the session's `TimeZone` and drops the
offset, so `09:30+00:00` comes back as `12:30` on a session set to
Africa/Nairobi. Two clients reporting the same instant in different offsets
store different instants, and the hash faithfully commits to whichever one
happened to be written. Nothing errors; the value is simply wrong.

Normalizing on the way in makes the column mean one thing — naive UTC, the
same convention `audit_log.created_at` already uses — for every writer, not
just the ones that remembered.
"""

import datetime as dt
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[dt.datetime]):
    """`TIMESTAMP WITHOUT TIME ZONE` that always stores naive UTC.

    Emits identical DDL to `DateTime`, so it is a drop-in on an existing column
    and needs no migration.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime):  # pragma: no cover - defensive
            return value
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC).replace(tzinfo=None)
        return value
