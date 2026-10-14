"""Staff account rules (13 §5, D2, D3).

One account per person and one role per account (03 §13.1(a)). The router owns
the HTTP and audit shape; the rules that must hold however an account is
changed live here, so a test can reach the guard that HTTP never can (the
two-admins race in `deactivate`).
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Role
from app.models import User
from app.services import refresh_tokens

# Lower-case, starting with a letter: a username can never be mistaken for a
# phone number, which login also accepts as an identifier.
USERNAME_PATTERN = r"^[a-z][a-z0-9._-]{2,39}$"


def valid_username(value: str) -> bool:
    return re.fullmatch(USERNAME_PATTERN, value) is not None


class Refused(Exception):
    """A rule refused the change. `code` is the stable API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def deactivate(db: Session, *, actor: User, target: User) -> bool:
    """Deactivate `target` and revoke every session it holds. Returns False
    when it was already inactive (nothing to do, nothing to audit).

    Three refusals, checked in this order:
    - your own account: an admin locking themselves out is never intended;
    - the root admin: the account `create_admin.py` made is the way back in
      when every other admin is gone;
    - the last active admin. Over HTTP the first two make this unreachable,
      because the caller is itself an active admin. It guards two admins
      deactivating each other at the same moment: the active admins are
      locked, so the second request waits, then sees it would leave none.
    """
    if not target.is_active:
        return False
    if target.id == actor.id:
        raise Refused("cannot_deactivate_self", "You can't deactivate your own account")
    if target.is_root:
        raise Refused("root_protected", "The first admin's account can't be deactivated")
    if target.role is Role.admin:
        others = (
            db.execute(
                select(User.id)
                .where(User.role == Role.admin, User.is_active.is_(True), User.id != target.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if not others:
            raise Refused("last_admin", "This is the last active admin")

    target.is_active = False
    refresh_tokens.revoke_all(db, target.id)
    db.flush()
    return True
