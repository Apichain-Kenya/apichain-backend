"""Auth + RBAC dependencies (P1-D, 04 §5.3).

`get_current_user` decodes the bearer access token and loads an active user
who has chosen their own password. `get_current_user_pending_ok` also admits
one who has not yet (13 D5); only `/v2/me` and `/v2/auth/password` use it.
`require_roles(*roles)` gates by role. `requires(action)` gates by a single
`ACTION_ROLES` table — the whole authorization surface in one auditable place,
so a reviewer reads permissions here instead of chasing decorators across
routers. Unknown actions raise at route-definition time (fail fast).
"""

from collections.abc import Callable

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import APIError
from app.models import Role, User
from app.services.security import decode_token

_bearer = HTTPBearer(auto_error=False)

# The complete action -> allowed-roles map. Grows one line per new action.
ACTION_ROLES: dict[str, set[Role]] = {
    "farmer.enroll": {Role.field_officer, Role.admin},
    # Grant or withdraw a consent purpose (P3b-B). Admits a farmer acting for
    # themselves, so the route calls ownership.assert_acts_for_farmer.
    "farmer.consent": {Role.farmer, Role.field_officer, Role.admin},
    # A farmer's identity documents (P3b-E). Operators and lab officers have no
    # reason to read them. Both admit a farmer, so both routes call
    # ownership.assert_acts_for_farmer.
    "document.upload": {Role.farmer, Role.field_officer, Role.admin},
    "document.read": {Role.farmer, Role.field_officer, Role.admin},
    # Enrolment contact verification (P3b-G): a verification step, never a
    # login (04 §3.5). Farmer-reachable, so both routes check ownership.
    "farmer.verify_contact": {Role.farmer, Role.field_officer, Role.admin},
    "farmer.confirm_contact": {Role.farmer, Role.field_officer, Role.admin},
    "apiary.create": {Role.farmer, Role.field_officer, Role.admin},
    # Reads exact hive coordinates, which are sensitive PII (03 §8.1, 12 D17):
    # the owner and the staff who enrol them, not operators or lab officers.
    # Admits a farmer, so the route calls ownership.assert_acts_for_farmer.
    "apiary.read": {Role.farmer, Role.field_officer, Role.admin},
    # Every role reads batches; a farmer only their own (ownership check in
    # the route). The apiary point is withheld from operators and lab officers.
    "batch.read": {Role.farmer, Role.field_officer, Role.operator, Role.lab_officer, Role.admin},
    "batch.create": {Role.farmer, Role.operator, Role.admin},
    # A farmer self-registers their own harvest (04 §5.3); the remaining four
    # transitions are staff actions. Attribution for all of them lives in
    # audit_log.actor_id/actor_role, which is what makes the single-dashboard
    # walk safe without losing who actually acted.
    "batch.harvest_record": {Role.farmer, Role.operator, Role.admin},
    "batch.process_record": {Role.operator, Role.admin},
    # No oracle contract to satisfy in v2 (01), so the lab officer signs their
    # own audit row rather than routing through a shared oracle key (04 §5.3).
    "batch.lab_verify": {Role.lab_officer, Role.admin},
    # Staff-only, so no farmer can reach them and no ownership check applies
    # (ownership.assert_acts_for_farmer guards only farmer-reachable actions).
    # Admit a farmer here and that check becomes mandatory.
    "batch.package": {Role.operator, Role.admin},
    "batch.distribute": {Role.operator, Role.admin},
    # Staff accounts (P4b-A): create, list, deactivate, reset. Admin only;
    # only the root admin may create another admin (checked in the route).
    "user.manage": {Role.admin},
    # Farmer list and profile (P4b-B, 13 D7): field officers and admins see
    # every farmer, contact details included. Admits a farmer for their own
    # profile only, so the profile route checks ownership and the list route
    # refuses a farmer outright. Operators and lab officers see a farmer's
    # name on a batch and nothing else.
    "farmer.read": {Role.farmer, Role.field_officer, Role.admin},
}


def get_current_user_pending_ok(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """The bearer, even if they must still change a password someone else
    chose. Only for the routes that let them do exactly that."""
    if credentials is None:
        raise APIError(401, "unauthorized", "Authentication required")
    try:
        claims = decode_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise APIError(401, "invalid_token", "Invalid or expired token") from exc
    if claims.get("type") != "access":
        raise APIError(401, "invalid_token", "Not an access token")
    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise APIError(401, "unauthorized", "User not found or inactive")
    return user


def get_current_user(user: User = Depends(get_current_user_pending_ok)) -> User:
    """The bearer, refused while someone else still knows their password
    (03 §13.1, 13 D5): anything they did would be recorded under their name
    by whoever chose it. The server enforces it, so no client can skip it."""
    if user.must_change_password:
        raise APIError(
            403, "password_change_required", "Choose your own password before continuing"
        )
    return user


def require_roles(*roles: Role) -> Callable[..., User]:
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise APIError(403, "forbidden", "Insufficient role")
        return user

    return dep


def requires(action: str) -> Callable[..., User]:
    try:
        allowed = ACTION_ROLES[action]
    except KeyError as exc:
        raise KeyError(f"Unknown RBAC action: {action}") from exc

    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise APIError(
                403,
                "forbidden",
                f"Role '{user.role}' may not perform '{action}'",
                {"action": action},
            )
        return user

    return dep
