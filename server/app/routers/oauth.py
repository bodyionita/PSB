"""MCP OAuth 2.1 authorization-server endpoints (M5 task 3, ADR-046 §2).

Mounted at the **root** (not under ``/api/v1``) so a connector reaches the spec-mandated paths
(Caddy proxies these to the api app — task 5): discovery, open DCR, the ``/authorize`` password +
consent gate, and ``/token``. All routes are public (this *is* the auth surface); the gate is the
password + explicit consent, not a session. The revoke-all switch lives on the session-gated admin
router.

Routers stay thin (rule 5): validation + delegation to :class:`OAuthService`; every flow decision,
crypto, and DB touch is the service's.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from authlib.common.security import generate_token
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import Settings
from ..dependencies import get_auth_service, get_oauth_service, get_settings
from ..oauth.consent import render_consent_page, render_error_page
from ..oauth.errors import AccessDenied, AuthorizeError, AuthorizeRedirectError, OAuthError
from ..oauth.metadata import authorization_server_metadata, protected_resource_metadata
from ..oauth.service import OAuthService
from ..services.auth_service import AuthService

router = APIRouter(tags=["oauth"])

# The double-submit CSRF cookie for the consent POST (defends the short-circuited, PWA-session
# consent from a cross-site auto-submit; SameSite=Lax already blocks cross-site POST cookie send).
CSRF_COOKIE = "mcp_oauth_csrf"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


# --- discovery ------------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    return JSONResponse(authorization_server_metadata(settings))


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(settings))


# --- Dynamic Client Registration (RFC 7591) -------------------------------------------------


@router.post("/register")
async def register(
    request: Request,
    service: OAuthService = Depends(get_oauth_service),
) -> JSONResponse:
    try:
        metadata = await request.json()
    except Exception:
        return _oauth_error_json(
            OAuthError("request body must be JSON", error="invalid_client_metadata", status=400)
        )
    if not isinstance(metadata, dict):
        return _oauth_error_json(
            OAuthError("metadata must be an object", error="invalid_client_metadata", status=400)
        )
    try:
        registered = await service.register_client(metadata)
    except OAuthError as exc:
        return _oauth_error_json(exc)

    body: dict[str, object] = {
        "client_id": registered.client_id,
        "client_id_issued_at": registered.client_id_issued_at,
        "token_endpoint_auth_method": registered.metadata["token_endpoint_auth_method"],
        "redirect_uris": registered.metadata["redirect_uris"],
        "grant_types": registered.metadata["grant_types"],
        "response_types": registered.metadata["response_types"],
        "scope": registered.metadata["scope"],
    }
    if registered.metadata.get("client_name"):
        body["client_name"] = registered.metadata["client_name"]
    if registered.client_secret is not None:
        body["client_secret"] = registered.client_secret
        body["client_secret_expires_at"] = 0  # non-expiring (RFC 7591)
    return JSONResponse(body, status_code=status.HTTP_201_CREATED, headers=_NO_STORE)


# --- /authorize -----------------------------------------------------------------------------


@router.get("/authorize")
async def authorize_get(
    request: Request,
    settings: Settings = Depends(get_settings),
    service: OAuthService = Depends(get_oauth_service),
    auth: AuthService = Depends(get_auth_service),
) -> Response:
    params = dict(request.query_params)
    try:
        req = await service.load_authorization_request(params)
    except AuthorizeError as exc:
        return HTMLResponse(render_error_page(exc.title, exc.message), status_code=400)
    except AuthorizeRedirectError as exc:
        return _redirect_error(exc)

    session = await auth.validate(request.cookies.get(settings.session_cookie_name))
    return _render_consent(req, settings, needs_password=session is None)


@router.post("/authorize")
async def authorize_post(
    request: Request,
    settings: Settings = Depends(get_settings),
    service: OAuthService = Depends(get_oauth_service),
    auth: AuthService = Depends(get_auth_service),
) -> Response:
    form = dict(await request.form())

    # CSRF: the double-submit cookie must match the hidden field (blocks cross-site auto-submit).
    cookie = request.cookies.get(CSRF_COOKIE)
    field = str(form.get("csrf_token") or "")
    if not cookie or not field or cookie != field:
        return HTMLResponse(
            render_error_page("Session expired", "Please restart the authorization from your app."),
            status_code=400,
        )

    try:
        req = await service.load_authorization_request(form)
    except AuthorizeError as exc:
        return HTMLResponse(render_error_page(exc.title, exc.message), status_code=400)
    except AuthorizeRedirectError as exc:
        return _redirect_error(exc)

    if str(form.get("decision") or "") != "approve":
        return _redirect_error(
            AuthorizeRedirectError(
                redirect_uri=req.redirect_uri,
                error=AccessDenied.error,
                description="the user denied access",
                state=req.state,
            )
        )

    # Rate-limit the authenticated decision (reuses the login limiter — ADR-046 §2).
    if not request.app.state.login_rate_limiter.allow(_client_ip(request)):
        return HTMLResponse(
            render_error_page("Too many attempts", "Please wait a moment and try again."),
            status_code=429,
        )

    session = await auth.validate(request.cookies.get(settings.session_cookie_name))
    if session is None:
        password = str(form.get("password") or "")
        if not auth.verify_password(password):
            return _render_consent(
                req, settings, needs_password=True, error="Incorrect password.", status_code=401
            )

    code = await service.issue_code(req)
    extra = {"code": code}
    if req.state:
        extra["state"] = req.state
    return RedirectResponse(_append_query(req.redirect_uri, extra), status_code=302)


# --- /token ---------------------------------------------------------------------------------


@router.post("/token")
async def token(
    request: Request,
    service: OAuthService = Depends(get_oauth_service),
) -> JSONResponse:
    form = dict(await request.form())
    grant_type = str(form.get("grant_type") or "")
    try:
        if grant_type == "authorization_code":
            grant = await service.exchange_code(
                code=str(form.get("code") or ""),
                client_id=str(form.get("client_id") or ""),
                redirect_uri=str(form.get("redirect_uri") or ""),
                code_verifier=(str(form["code_verifier"]) if "code_verifier" in form else None),
                client_secret=(str(form["client_secret"]) if "client_secret" in form else None),
            )
        elif grant_type == "refresh_token":
            grant = await service.refresh(
                refresh_token=str(form.get("refresh_token") or ""),
                client_id=str(form.get("client_id") or ""),
                client_secret=(str(form["client_secret"]) if "client_secret" in form else None),
            )
        else:
            return _oauth_error_json(
                OAuthError(
                    f"unsupported grant_type: {grant_type!r}",
                    error="unsupported_grant_type",
                    status=400,
                )
            )
    except OAuthError as exc:
        return _oauth_error_json(exc)
    return JSONResponse(grant.to_dict(), headers=_NO_STORE)


# --- helpers --------------------------------------------------------------------------------


def _render_consent(
    req,
    settings: Settings,
    *,
    needs_password: bool,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    csrf = generate_token(24)
    html = render_consent_page(
        app_name=settings.app_name,
        client_name=req.client_name,
        scope=req.scope,
        needs_password=needs_password,
        csrf_token=csrf,
        fields=req.carried_fields(),
        error=error,
    )
    response = HTMLResponse(html, status_code=status_code)
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=600,
        path="/authorize",
    )
    return response


def _redirect_error(exc: AuthorizeRedirectError) -> RedirectResponse:
    extra = {"error": exc.error}
    if exc.description:
        extra["error_description"] = exc.description
    if exc.state:
        extra["state"] = exc.state
    return RedirectResponse(_append_query(exc.redirect_uri, extra), status_code=302)


def _oauth_error_json(exc: OAuthError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.status, headers=_NO_STORE)


def _append_query(uri: str, extra: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(extra.items())
    return urlunsplit(parts._replace(query=urlencode(query)))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
