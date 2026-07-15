"""OAuth router tests (M5 task 3, ADR-046 §2) — discovery, DCR, the /authorize consent gate
(CSRF + password + PKCE), and /token — driven end-to-end over the real service + a fake store.
"""

from __future__ import annotations

from datetime import UTC, datetime

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_auth_service, get_oauth_service, get_settings
from app.oauth.service import OAuthService
from app.routers import oauth
from app.services.auth_service import SessionInfo
from app.services.rate_limit import RateLimiter

from .test_oauth_service import REDIRECT, VERIFIER, Clock, FakeStore

CHALLENGE = create_s256_code_challenge(VERIFIER)


class FakeAuth:
    def __init__(self, *, password: str = "hunter2", session: SessionInfo | None = None) -> None:
        self._password = password
        self._session = session

    def verify_password(self, password: str) -> bool:
        return password == self._password

    async def validate(self, token: str | None) -> SessionInfo | None:
        return self._session


def _settings() -> Settings:
    return Settings(
        public_base_url="https://example.test",
        mcp_token_hmac_secret="test-secret",
        api_password_hash="",
        session_cookie_name="braindan_session",
    )


def _build(*, auth: FakeAuth | None = None, limiter: RateLimiter | None = None):
    settings = _settings()
    store = FakeStore(Clock())
    service = OAuthService(settings=settings, store=store, clock=Clock())
    app = FastAPI()
    app.include_router(oauth.router)
    app.state.login_rate_limiter = limiter or RateLimiter(max_events=100, window_seconds=60.0)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_service] = lambda: service
    app.dependency_overrides[get_auth_service] = lambda: (auth or FakeAuth())
    return TestClient(app), service


def _authorize_query(client_id: str, **overrides) -> dict:
    q = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    q.update(overrides)
    return q


async def _register(service: OAuthService) -> str:
    reg = await service.register_client({"redirect_uris": [REDIRECT], "client_name": "Claude"})
    return reg.client_id


# --- discovery -------------------------------------------------------------------------------


def test_authorization_server_metadata():
    client, _ = _build()
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"] == "https://example.test"
    assert body["authorization_endpoint"] == "https://example.test/authorize"
    assert body["token_endpoint"] == "https://example.test/token"
    assert body["registration_endpoint"] == "https://example.test/register"
    assert body["code_challenge_methods_supported"] == ["S256"]


def test_protected_resource_metadata():
    client, _ = _build()
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://example.test/mcp"
    assert body["authorization_servers"] == ["https://example.test"]


# --- DCR -------------------------------------------------------------------------------------


def test_register_public_client_201():
    client, _ = _build()
    resp = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"].startswith("mcp_")
    assert "client_secret" not in body  # public client
    assert body["redirect_uris"] == [REDIRECT]
    assert resp.headers["cache-control"] == "no-store"


def test_register_rejects_bad_metadata():
    client, _ = _build()
    resp = client.post("/register", json={"redirect_uris": []})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_register_rejects_non_json():
    client, _ = _build()
    resp = client.post("/register", content=b"not json", headers={"content-type": "text/plain"})
    assert resp.status_code == 400


# --- /authorize GET --------------------------------------------------------------------------


def test_authorize_get_renders_consent():
    client, service = _build()
    client_id = _run(_register(service))
    resp = client.get("/authorize", params=_authorize_query(client_id))
    assert resp.status_code == 200
    assert "Approve" in resp.text and "Deny" in resp.text
    # No PWA session (FakeAuth default) ⇒ the password field is shown.
    assert 'type="password"' in resp.text
    assert client.cookies.get("mcp_oauth_csrf")


def test_authorize_get_unknown_client_error_page():
    client, _ = _build()
    resp = client.get("/authorize", params=_authorize_query("nope"))
    assert resp.status_code == 400
    assert "Unknown client" in resp.text


def test_authorize_get_bad_pkce_redirects():
    client, service = _build()
    client_id = _run(_register(service))
    q = _authorize_query(client_id)
    del q["code_challenge"]
    resp = client.get("/authorize", params=q, follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(REDIRECT)
    assert "error=invalid_request" in loc
    assert "state=xyz" in loc


def test_authorize_get_session_hides_password():
    session = SessionInfo(id="s1", created_at=datetime.now(UTC))
    client, service = _build(auth=FakeAuth(session=session))
    client_id = _run(_register(service))
    resp = client.get("/authorize", params=_authorize_query(client_id))
    assert resp.status_code == 200
    assert 'type="password"' not in resp.text  # PWA session short-circuits password


# --- /authorize POST -------------------------------------------------------------------------


def _consent_and_form(client: TestClient, client_id: str) -> dict:
    """GET the consent page (sets the CSRF cookie), then build a matching POST form."""
    client.get("/authorize", params=_authorize_query(client_id))
    csrf = client.cookies.get("mcp_oauth_csrf")
    return {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "brain",
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": "https://example.test/mcp",
        "csrf_token": csrf,
    }


def test_authorize_post_approve_with_password_issues_code():
    client, service = _build(auth=FakeAuth(password="hunter2"))
    client_id = _run(_register(service))
    form = _consent_and_form(client, client_id)
    form["decision"] = "approve"
    form["password"] = "hunter2"
    resp = client.post("/authorize", data=form, follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(REDIRECT + "?") or loc.startswith(REDIRECT + "&")
    assert "code=" in loc and "state=xyz" in loc


def test_authorize_post_wrong_password_reprompts_401():
    client, service = _build(auth=FakeAuth(password="hunter2"))
    client_id = _run(_register(service))
    form = _consent_and_form(client, client_id)
    form["decision"] = "approve"
    form["password"] = "wrong"
    resp = client.post("/authorize", data=form)
    assert resp.status_code == 401
    assert "Incorrect password" in resp.text


def test_authorize_post_deny_redirects_access_denied():
    client, service = _build()
    client_id = _run(_register(service))
    form = _consent_and_form(client, client_id)
    form["decision"] = "deny"
    form["password"] = "hunter2"
    resp = client.post("/authorize", data=form, follow_redirects=False)
    assert resp.status_code == 302
    assert "error=access_denied" in resp.headers["location"]


def test_authorize_post_csrf_mismatch_400():
    client, service = _build()
    client_id = _run(_register(service))
    form = _consent_and_form(client, client_id)
    form["csrf_token"] = "tampered"
    form["decision"] = "approve"
    form["password"] = "hunter2"
    resp = client.post("/authorize", data=form)
    assert resp.status_code == 400
    assert "Session expired" in resp.text


def test_authorize_post_session_skips_password():
    session = SessionInfo(id="s1", created_at=datetime.now(UTC))
    client, service = _build(auth=FakeAuth(session=session))
    client_id = _run(_register(service))
    form = _consent_and_form(client, client_id)
    form["decision"] = "approve"  # no password supplied — session short-circuits it
    resp = client.post("/authorize", data=form, follow_redirects=False)
    assert resp.status_code == 302
    assert "code=" in resp.headers["location"]


# --- /token ----------------------------------------------------------------------------------


def _full_flow_code(client: TestClient, service: OAuthService, client_id: str) -> str:
    form = _consent_and_form(client, client_id)
    form["decision"] = "approve"
    form["password"] = "hunter2"
    resp = client.post("/authorize", data=form, follow_redirects=False)
    loc = resp.headers["location"]
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(loc).query)["code"][0]


def test_token_code_exchange():
    client, service = _build(auth=FakeAuth(password="hunter2"))
    client_id = _run(_register(service))
    code = _full_flow_code(client, service, client_id)
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] == 3600
    assert resp.headers["cache-control"] == "no-store"


def test_token_bad_verifier_invalid_grant():
    client, service = _build(auth=FakeAuth(password="hunter2"))
    client_id = _run(_register(service))
    code = _full_flow_code(client, service, client_id)
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": "b" * 64,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_unsupported_grant():
    client, _ = _build()
    resp = client.post("/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_token_refresh_rotation():
    client, service = _build(auth=FakeAuth(password="hunter2"))
    client_id = _run(_register(service))
    code = _full_flow_code(client, service, client_id)
    first = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
    ).json()
    resp = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"] != first["access_token"]


# --- test helper: run a coroutine synchronously (TestClient is sync) --------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)
