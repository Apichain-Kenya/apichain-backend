"""Enrolment contact-verification codes: generation, HMAC, limits (P3b-G, 11 §9).

A *verification* step inside enrolment ("confirm this number is yours", 03 §9),
never a login (04 §3.5). v1 removed OTP-login and nothing here brings it back.

**Stored as an HMAC, never as the code.** Six digits is a 10^6 space; an
unkeyed hash of one is reversed by enumeration in milliseconds by anyone who
can read the table. The key is `verification_code_pepper`, and the farmer and
channel are bound into the MAC so a row cannot be replayed against another.

**Limits (11 D13), counted from this table under the farmer's row lock:**
a 60-second cooldown and five sends per hour per (farmer, channel), and five
wrong attempts per code. Failed sends count too, so a provider outage cannot
be used to send unlimited codes. Per-IP limits stay in Phase 5.

**One clock.** Expiry and the rate windows are computed and compared in
Python against `utcnow()`, stored as naive UTC by `UtcDateTime`. Nothing here
compares a Python value with a SQL `now()`, which is the 3a timezone bug.
"""

import datetime as dt
import hashlib
import hmac
import math
import secrets
from collections.abc import Sequence

from app.config import settings
from app.enums import CommChannel

CODE_TTL = dt.timedelta(minutes=10)
COOLDOWN = dt.timedelta(seconds=60)
WINDOW = dt.timedelta(hours=1)
SENDS_PER_WINDOW = 5
MAX_ATTEMPTS = 5


def utcnow() -> dt.datetime:
    """Naive UTC, the form every column here stores. Tests replace this."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def generate() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def mac(farmer_id: int, channel: CommChannel, code: str) -> bytes:
    message = f"{farmer_id}|{channel}|{code}".encode()
    return hmac.new(settings.verification_code_pepper.encode(), message, hashlib.sha256).digest()


def matches(stored: bytes, farmer_id: int, channel: CommChannel, code: str) -> bool:
    return hmac.compare_digest(stored, mac(farmer_id, channel, code))


def retry_after(recent: Sequence[dt.datetime], now: dt.datetime) -> int | None:
    """Seconds until another send is allowed, or None if one is allowed now.

    `recent` is the creation time of every code for this (farmer, channel)
    inside the last hour, newest first.
    """
    waits: list[float] = []
    if recent and now - recent[0] < COOLDOWN:
        waits.append((recent[0] + COOLDOWN - now).total_seconds())
    if len(recent) >= SENDS_PER_WINDOW:
        oldest_counted = recent[SENDS_PER_WINDOW - 1]
        waits.append((oldest_counted + WINDOW - now).total_seconds())
    if not waits:
        return None
    return max(1, math.ceil(max(waits)))
