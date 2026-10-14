"""Staff accounts (P4b-A, 13 §5, D2, D5).

Admin only. One account per person and one role per account (03 §13.1(a)): a
person who changes jobs gets a new account and the old one is deactivated, so
there is no role change and no shared login. An account made or reset here has
a password its admin chose, so its owner must choose their own at the next
sign-in (D5). No password reaches a fingerprint or an audit payload (D4).
"""

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import requires
from app.enums import Role
from app.errors import APIError, error_responses
from app.models import User
from app.routers._context import request_context
from app.schemas.common import IdempotencyKeyHeader
from app.schemas.users import PasswordResetRequest, UserCreateRequest, UserOut, UserPage
from app.services import audit_log, idempotency, password_policy, refresh_tokens, security, users

router = APIRouter(prefix="/users", tags=["users"])
_require_manage = requires("user.manage")

_USER_ID = Path(ge=1, le=2_147_483_647)
_PASSWORD = frozenset({"password"})


def _user_or_404(db: Session, user_id: int) -> User:
    # Locked: a deactivation and a reset of one account must not interleave.
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
    if user is None:
        raise APIError(404, "user_not_found", "User does not exist", {"user_id": user_id})
    return user


@router.post(
    "",
    response_model=UserOut,
    status_code=201,
    responses=error_responses(401, 403, 409, 422),
)
def create_user(
    body: UserCreateRequest,
    request: Request,
    idempotency_key: IdempotencyKeyHeader,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_manage),
) -> UserOut | JSONResponse:
    idem = idempotency.begin(
        db,
        key=idempotency_key,
        actor_id=actor.id,
        body=body.model_dump(mode="json"),
        exclude=_PASSWORD,
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    role = Role(body.role)
    if role is Role.admin and not actor.is_root:
        raise APIError(403, "root_required", "Only the first admin can add another admin")
    password_policy.check(role, body.password)
    if db.execute(select(User.id).where(User.username == body.username)).first() is not None:
        raise APIError(409, "username_taken", "That username is taken")

    user = User(
        username=body.username,
        full_name=body.full_name,
        password_hash=security.hash_password(body.password),
        role=role,
        is_root=False,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    db.refresh(user)  # server defaults (created_at), before the audit append

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="user",
        subject_id=str(user.id),
        action="user.created",
        # No password and no full name: the name lives in `users`, not in a
        # row that can never be deleted (minimisation).
        payload={
            "user_id": user.id,
            "username": user.username,
            "role": str(role),
            "created_by": actor.id,
        },
        ip=ip,
        user_agent=user_agent,
    )
    response = UserOut.model_validate(user)
    idempotency.finish(db, idem, status_code=201, body=response.model_dump(mode="json"))
    db.commit()
    return response


@router.get("", response_model=UserPage, responses=error_responses(401, 403))
def list_users(
    role: Role | None = None,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, pattern=r"^[1-9][0-9]{0,9}$"),
    db: Session = Depends(get_db),
    actor: User = Depends(_require_manage),
) -> UserPage:
    """Staff accounts, newest first. Farmers are listed under `/v2/farmers`."""
    query = select(User).where(User.role != Role.farmer)
    if role is not None:
        query = query.where(User.role == role)
    if active is not None:
        query = query.where(User.is_active.is_(active))
    if cursor is not None:
        query = query.where(User.id < int(cursor))
    rows = db.execute(query.order_by(User.id.desc()).limit(limit + 1)).scalars().all()
    page = rows[:limit]
    return UserPage(
        items=[UserOut.model_validate(u) for u in page],
        next_cursor=str(page[-1].id) if len(rows) > limit else None,
    )


@router.post(
    "/{user_id}/deactivate",
    response_model=UserOut,
    responses=error_responses(401, 403, 404, 409),
)
def deactivate_user(
    request: Request,
    idempotency_key: IdempotencyKeyHeader,
    user_id: int = _USER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_manage),
) -> UserOut | JSONResponse:
    idem = idempotency.begin(db, key=idempotency_key, actor_id=actor.id, body={"user_id": user_id})
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    target = _user_or_404(db, user_id)
    try:
        changed = users.deactivate(db, actor=actor, target=target)
    except users.Refused as exc:
        raise APIError(409, exc.code, exc.message) from exc

    if changed:
        ip, user_agent = request_context(request)
        audit_log.append(
            db,
            actor_id=actor.id,
            actor_role=actor.role,
            subject_type="user",
            subject_id=str(target.id),
            action="user.deactivated",
            payload={"user_id": target.id, "deactivated_by": actor.id},
            ip=ip,
            user_agent=user_agent,
        )
    response = UserOut.model_validate(target)
    idempotency.finish(db, idem, status_code=200, body=response.model_dump(mode="json"))
    db.commit()
    return response


@router.post(
    "/{user_id}/reset-password",
    response_model=UserOut,
    responses=error_responses(401, 403, 404, 422),
)
def reset_password(
    body: PasswordResetRequest,
    request: Request,
    idempotency_key: IdempotencyKeyHeader,
    user_id: int = _USER_ID,
    db: Session = Depends(get_db),
    actor: User = Depends(_require_manage),
) -> UserOut | JSONResponse:
    """Set a new starting password for anyone, a farmer included (a farmer who
    forgot theirs asks an admin). The owner must change it at their next
    sign-in, and every session they hold ends now."""
    idem = idempotency.begin(
        db,
        key=idempotency_key,
        actor_id=actor.id,
        body={"user_id": user_id, **body.model_dump(mode="json")},
        exclude=_PASSWORD,
    )
    if idem.replay is not None:
        return JSONResponse(status_code=idem.replay.status_code, content=idem.replay.body)

    target = _user_or_404(db, user_id)
    # The rule is the target's: a farmer may be given eight characters.
    password_policy.check(target.role, body.password)
    target.password_hash = security.hash_password(body.password)
    target.must_change_password = True
    refresh_tokens.revoke_all(db, target.id)
    db.flush()

    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=actor.id,
        actor_role=actor.role,
        subject_type="user",
        subject_id=str(target.id),
        action="user.password_reset",
        payload={"user_id": target.id, "reset_by": actor.id},
        ip=ip,
        user_agent=user_agent,
    )
    response = UserOut.model_validate(target)
    idempotency.finish(db, idem, status_code=200, body=response.model_dump(mode="json"))
    db.commit()
    return response
