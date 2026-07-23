"""Auth + RBAC dependencies (P1-D, 04 §5.3).

`get_current_user` decodes the bearer access token and loads an active user.
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
    "batch.create": {Role.farmer, Role.operator, Role.admin},
}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
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
