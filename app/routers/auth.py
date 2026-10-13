"""Auth endpoints (P1-F, reworked P4a-A): login, refresh (rotation), logout,
and `GET /v2/me`. Excluded from the idempotency layer — rotation/reuse owns
their retry semantics (08 D6).

The refresh token lives only in the `__Secure-apichain_rt` cookie (12 D3):
`HttpOnly` so no script can read it, `Secure`, `SameSite=Strict`, and scoped
to `/v2/auth` so it rides on nothing but these routes. `__Secure-` rather than
`__Host-`, because `__Host-` forces `Path=/` and would send it everywhere.
Refresh and logout also require `X-ApiChain-Client: pwa`: a cross-site form
cannot set a custom header, and a cross-origin fetch that does is preflighted.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.errors import APIError, error_responses
from app.models import Farmer, User
from app.routers._context import request_context
from app.schemas.auth import AccessTokenResponse, LoginRequest, MeResponse, StatusResponse
from app.services import audit_log, refresh_tokens, security

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "__Secure-apichain_rt"
_COOKIE_PATH = "/v2/auth"

RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE)]
# A plain optional string in the signature, so any absent or wrong value is the
# same stable 403 envelope from `_require_client_header` rather than a 422 (a
# Literal here made Schemathesis's schema-valid "null" string a 422).
ClientHeader = Annotated[str | None, Header(alias="X-ApiChain-Client")]


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        path=_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=_COOKIE_PATH, secure=True, httponly=True, samesite="strict"
    )


def _require_client_header(value: str | None) -> None:
    if value != "pwa":
        raise APIError(403, "client_header_required", "X-ApiChain-Client header required")


def _access(user: User) -> AccessTokenResponse:
    return AccessTokenResponse(
        access_token=security.create_access_token(sub=user.id, role=user.role),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/login", response_model=AccessTokenResponse, responses=error_responses(401))
def login(
    body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> AccessTokenResponse:
    user = db.execute(
        select(User).where(
            or_(User.username == body.identifier, User.phone == body.identifier),
            User.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise APIError(401, "invalid_credentials", "Invalid credentials")

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
    _set_refresh_cookie(response, raw_refresh)
    return _access(user)


@router.post("/refresh", response_model=AccessTokenResponse, responses=error_responses(401, 403))
def refresh(
    request: Request,
    response: Response,
    refresh_cookie: RefreshCookie = None,
    client_header: ClientHeader = None,
    db: Session = Depends(get_db),
) -> AccessTokenResponse:
    _require_client_header(client_header)
    if not refresh_cookie:
        raise APIError(401, "invalid_token", "Invalid or expired refresh token")
    try:
        rotation = refresh_tokens.rotate(db, refresh_cookie)
    except refresh_tokens.ReuseDetected as exc:
        db.commit()  # persist the family revocation before failing
        raise APIError(
            401, "token_reuse", "Refresh token reuse detected; sessions revoked"
        ) from exc
    except refresh_tokens.InvalidRefreshToken as exc:
        raise APIError(401, "invalid_token", "Invalid or expired refresh token") from exc

    user = db.get(User, rotation.row.user_id)
    assert user is not None
    if not user.is_active:
        # As login refuses an inactive account, so does refresh: a
        # deactivated user's cookie must not keep renewing itself.
        refresh_tokens.revoke_all(db, user.id)
        db.commit()
        raise APIError(401, "invalid_token", "Invalid or expired refresh token")
    if rotation.grace:
        # The one path where a spent token worked: on the record (12 D5).
        ip, user_agent = request_context(request)
        audit_log.append(
            db,
            actor_id=user.id,
            actor_role=user.role,
            subject_type="user",
            subject_id=str(user.id),
            action="auth.refresh_reuse_grace",
            payload={"user_id": user.id},
            ip=ip,
            user_agent=user_agent,
        )
    db.commit()
    _set_refresh_cookie(response, rotation.raw)
    return _access(user)


@router.post("/logout", response_model=StatusResponse, responses=error_responses(403))
def logout(
    response: Response,
    refresh_cookie: RefreshCookie = None,
    client_header: ClientHeader = None,
    db: Session = Depends(get_db),
) -> StatusResponse:
    _require_client_header(client_header)
    if refresh_cookie:
        refresh_tokens.revoke(db, refresh_cookie)
        db.commit()
    _clear_refresh_cookie(response)
    return StatusResponse(status="logged_out")


@me_router.get("/me", response_model=MeResponse, responses=error_responses(401))
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    farmer = db.execute(select(Farmer).where(Farmer.user_id == user.id)).scalar_one_or_none()
    if farmer is not None:
        display_name = f"{farmer.first_name} {farmer.last_name}"
    else:
        display_name = user.username or user.phone or f"user {user.id}"
    return MeResponse(
        user_id=user.id,
        role=user.role,
        display_name=display_name,
        farmer_id=farmer.id if farmer is not None else None,
    )
