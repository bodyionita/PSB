"""Auth router (ADR-007): login / logout / me. Login is rate-limited per IP."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..config import Settings
from ..dependencies import get_auth_service, get_settings, require_session
from ..models import LoginRequest, LoginResponse, MeResponse
from ..services.auth_service import AuthService, InvalidCredentials, SessionInfo

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    # Behind Cloudflare/Caddy the real client is in X-Forwarded-For; fall back to peer.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_days * 24 * 3600,
        path="/",
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    limiter = request.app.state.login_rate_limiter
    if not limiter.allow(_client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again shortly.",
        )
    try:
        token = await auth.login(payload.password, user_agent=request.headers.get("user-agent"))
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password"
        ) from None
    _set_session_cookie(response, token, settings)
    return LoginResponse()


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, bool]:
    token = request.cookies.get(settings.session_cookie_name)
    await auth.logout(token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(session: SessionInfo = Depends(require_session)) -> MeResponse:
    return MeResponse(authenticated=True, session_created_at=session.created_at)
