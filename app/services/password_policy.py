"""The one password rule (04 §5.8, 13 D16).

Every *new* password goes through `check`: farmer enrolment, account creation,
an admin reset, a self-service change and the bootstrap script. Login does not,
so an account made before the rule keeps working until its next change.

Staff need 15 characters: NIST SP 800-63B-4 sets 15 for a password that is the
only factor, and every staff password here is. Farmers need 8, a deliberate and
documented departure from that floor for low-literacy users typing on a phone;
a farmer's account reaches only their own records. No composition rules (the
same revision forbids them). bcrypt reads only the first 72 bytes, so a longer
password is refused rather than silently truncated.
"""

from app.enums import Role
from app.errors import APIError

STAFF_MIN_LENGTH = 15
FARMER_MIN_LENGTH = 8
MAX_BYTES = 72


def min_length(role: Role) -> int:
    return FARMER_MIN_LENGTH if role is Role.farmer else STAFF_MIN_LENGTH


def check(role: Role, value: str) -> None:
    """Raise 422 `password_too_short` / `password_too_long` if `value` may not
    become the password of an account with `role`."""
    needed = min_length(role)
    if len(value) < needed:
        raise APIError(
            422,
            "password_too_short",
            f"Password must be at least {needed} characters",
            {"min_length": needed},
        )
    if len(value.encode("utf-8")) > MAX_BYTES:
        raise APIError(
            422,
            "password_too_long",
            f"Password must be at most {MAX_BYTES} bytes",
            {"max_bytes": MAX_BYTES},
        )
