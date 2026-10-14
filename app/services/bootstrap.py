"""The first admin (13 D3, 04 §5.3).

The only way an `is_root` account is made. There is no HTTP route for it: an
unauthenticated account-creation endpoint would sit on the public internet
until someone remembered to switch it off. This runs from
`scripts/create_admin.py` inside the api container, and shell access to the
container is the strongest credential the deployment already has.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Role
from app.errors import APIError
from app.models import User
from app.services import audit_log, password_policy, refresh_tokens, security, users


class BootstrapRefused(Exception):
    pass


def create_root_admin(db: Session, *, username: str, full_name: str, password: str) -> int:
    """Create the root admin and return its id; the caller commits.

    Refuses when any root admin exists, or when the password or username
    would be refused for an ordinary staff account. The root admin chose their
    own password, so they are not asked to change it.
    """
    if db.execute(select(User.id).where(User.is_root.is_(True))).first() is not None:
        raise BootstrapRefused("a root admin already exists")
    try:
        password_policy.check(Role.admin, password)
    except APIError as exc:
        raise BootstrapRefused(exc.message) from exc
    if not users.valid_username(username):
        raise BootstrapRefused(
            "username must be 3-40 lower-case characters, starting with a letter"
        )
    if not full_name.strip() or len(full_name) > 120:
        raise BootstrapRefused("full name is required (at most 120 characters)")
    if users.identifier_taken(db, username):
        raise BootstrapRefused(f"username {username!r} is taken")

    user = User(
        username=username,
        full_name=full_name,
        password_hash=security.hash_password(password),
        role=Role.admin,
        is_root=True,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    audit_log.append(
        db,
        actor_id=None,
        actor_role="system",
        subject_type="user",
        subject_id=str(user.id),
        action="user.bootstrapped",
        payload={"user_id": user.id, "username": username},
    )
    return user.id


def reset_root_password(db: Session, *, password: str) -> int:
    """Give the root admin a new password; the caller commits.

    The console refuses to reset the root admin, so a root admin who forgot
    their password is recovered here, from the server. They typed the new
    password at the prompt themselves, so no change is forced. Every session
    they hold ends, and the reset is audited as "system".
    """
    root = db.execute(
        select(User).where(User.is_root.is_(True)).with_for_update()
    ).scalar_one_or_none()
    if root is None:
        raise BootstrapRefused("there is no root admin; run create_admin.py")
    try:
        password_policy.check(Role.admin, password)
    except APIError as exc:
        raise BootstrapRefused(exc.message) from exc
    root.password_hash = security.hash_password(password)
    root.must_change_password = False
    root.is_active = True
    refresh_tokens.revoke_all(db, root.id)
    db.flush()
    audit_log.append(
        db,
        actor_id=None,
        actor_role="system",
        subject_type="user",
        subject_id=str(root.id),
        action="user.password_reset",
        payload={"user_id": root.id, "reset_by": None},
    )
    return root.id
