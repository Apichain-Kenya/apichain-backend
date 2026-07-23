"""Auth endpoints (P1-F): login, refresh (rotation), logout. Excluded from the
idempotency layer — rotation/reuse owns their retry semantics (08 D6)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import APIError, error_responses
from app.models import User
from app.routers._context import request_context
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    StatusResponse,
    TokenPair,
)
from app.services import audit_log, refresh_tokens, security

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair, responses=error_responses(401))
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    user = db.execute(
        select(User).where(
            or_(User.username == body.identifier, User.phone == body.identifier),
            User.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise APIError(401, "invalid_credentials", "Invalid credentials")

    access = security.create_access_token(sub=user.id, role=user.role)
    raw_refresh, _ = refresh_tokens.issue(db, user)
    ip, user_agent = request_context(request)
    audit_log.append(
        db,
        actor_id=user.id,
        actor_role=user.role,
        subject_type="user",
        subject_id=str(user.id),
        action="auth.login",
        payload={"user_id": user.id},
        ip=ip,
        user_agent=user_agent,
    )
    db.commit()
    return TokenPair(access_token=access, refresh_token=raw_refresh)


@router.post("/refresh", response_model=TokenPair, responses=error_responses(401))
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    try:
        new_raw, new_row = refresh_tokens.rotate(db, body.refresh_token)
    except refresh_tokens.ReuseDetected as exc:
        db.commit()  # persist the family revocation before failing
        raise APIError(
            401, "token_reuse", "Refresh token reuse detected; sessions revoked"
        ) from exc
    except refresh_tokens.InvalidRefreshToken as exc:
        raise APIError(401, "invalid_token", "Invalid or expired refresh token") from exc

    user = db.get(User, new_row.user_id)
    assert user is not None
    access = security.create_access_token(sub=user.id, role=user.role)
    db.commit()
    return TokenPair(access_token=access, refresh_token=new_raw)


@router.post("/logout", response_model=StatusResponse, responses=error_responses())
def logout(body: LogoutRequest, db: Session = Depends(get_db)) -> StatusResponse:
    refresh_tokens.revoke(db, body.refresh_token)
    db.commit()
    return StatusResponse(status="logged_out")
