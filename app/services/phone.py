"""Kenyan MSISDN normalization to E.164 (P3b-D, 11 D14). Pure.

`FarmerEnrollRequest.phone` has been an unconstrained string since Phase 1.
Tightening it would break a merged contract for a gain only the SMS path
needs, so the format is enforced where it matters: immediately before a send.
An unsendable number gets a defined outcome there (a 422 on the verification
endpoint, a `failed`/`invalid_recipient` row for a milestone), never a provider
error.

Kenya only, by design: the platform serves Kenyan smallholders, and Africa's
Talking bills Kenyan numbers. Mobile numbers are `7XXXXXXXX` (Safaricom,
Airtel, Telkom) and the newer `1XXXXXXXX` range, nine digits after `+254`.
"""

import re

_SEPARATORS = re.compile(r"[\s\-().]")
_FORMS = (
    re.compile(r"^\+254([17]\d{8})$"),
    re.compile(r"^254([17]\d{8})$"),
    re.compile(r"^0([17]\d{8})$"),
)


def to_e164_ke(raw: str | None) -> str | None:
    """`+2547XXXXXXXX` / `+2541XXXXXXXX`, or None if it is not a Kenyan mobile."""
    if not raw:
        return None
    compact = _SEPARATORS.sub("", raw)
    for form in _FORMS:
        match = form.match(compact)
        if match:
            return "+254" + match.group(1)
    return None
