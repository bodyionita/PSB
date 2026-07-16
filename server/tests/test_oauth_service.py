"""OAuth 2.1 authorization-server flow tests (M5 task 3, ADR-046 §2).

Fakes the :class:`OAuthStore` (in-memory, with the real atomic single-use + expiry semantics) so
the whole flow — DCR, authorize validation, PKCE code exchange, refresh rotation + reuse
detection, token validation, and revoke-all — is exercised with no DB (08 testing policy).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from app.config import Settings
from app.oauth.errors import (
    AuthorizeError,
    AuthorizeRedirectError,
    InvalidClient,
    InvalidClientMetadata,
    InvalidGrant,
)
from app.oauth.service import OAuthService
from app.oauth.store import AuthCodeRecord, ClientRecord, TokenRecord

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64  # a valid 43–128 char PKCE verifier
CHALLENGE = create_s256_code_challenge(VERIFIER)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now = self.now + timedelta(**kw)


class FakeStore:
    """In-memory OAuthStore with the real atomic-consume + expiry behaviour the service needs."""

    def __init__(self, clock: Clock) -> None:
        self._now = clock
        self.clients: dict[str, ClientRecord] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self._seq = 0

    async def create_client(self, *, client_id, client_secret_hash, metadata) -> None:
        self.clients[client_id] = ClientRecord(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            metadata=metadata,
            created_at=self._now(),
        )

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def create_code(
        self,
        *,
        code_hash,
        client_id,
        redirect_uri,
        code_challenge,
        code_challenge_method,
        scope,
        resource,
        expires_at,
    ) -> None:
        self.codes[code_hash] = dict(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            resource=resource,
            expires_at=expires_at,
            consumed_at=None,
        )

    async def consume_code(self, code_hash):
        row = self.codes.get(code_hash)
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= self._now():
            return None
        row["consumed_at"] = self._now()
        return AuthCodeRecord(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            code_challenge_method=row["code_challenge_method"],
            scope=row["scope"],
            resource=row["resource"],
        )

    async def consumed_code_client(self, code_hash):
        row = self.codes.get(code_hash)
        return row["client_id"] if row and row["consumed_at"] is not None else None

    async def create_token(self, *, client_id, token_hash, kind, scope, resource, expires_at):
        self._seq += 1
        tid = f"tok-{self._seq}"
        self.tokens[token_hash] = dict(
            id=tid,
            client_id=client_id,
            kind=kind,
            scope=scope,
            resource=resource,
            expires_at=expires_at,
            revoked_at=None,
        )
        return tid

    async def get_token(self, token_hash):
        row = self.tokens.get(token_hash)
        if row is None:
            return None
        return TokenRecord(
            id=row["id"],
            client_id=row["client_id"],
            kind=row["kind"],
            scope=row["scope"],
            resource=row["resource"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    async def touch_token(self, token_hash) -> None:
        pass

    async def revoke_token(self, token_hash) -> int:
        row = self.tokens.get(token_hash)
        if row and row["revoked_at"] is None:
            row["revoked_at"] = self._now()
            return 1
        return 0

    async def invalidate_all_codes(self) -> int:
        n = 0
        for row in self.codes.values():
            if row["consumed_at"] is None:
                row["consumed_at"] = self._now()
                n += 1
        return n

    async def revoke_client_tokens(self, client_id) -> int:
        n = 0
        for row in self.tokens.values():
            if row["client_id"] == client_id and row["revoked_at"] is None:
                row["revoked_at"] = self._now()
                n += 1
        return n

    async def revoke_all(self) -> int:
        n = 0
        for row in self.tokens.values():
            if row["revoked_at"] is None:
                row["revoked_at"] = self._now()
                n += 1
        return n


def _settings() -> Settings:
    return Settings(
        public_base_url="https://example.test",
        mcp_token_hmac_secret="test-secret",
        api_password_hash="",
    )


@pytest.fixture
def ctx():
    clock = Clock()
    store = FakeStore(clock)
    service = OAuthService(settings=_settings(), store=store, clock=clock)
    return service, store, clock


async def _register(service, *, confidential: bool = False):
    md = {"redirect_uris": [REDIRECT], "client_name": "Claude"}
    if confidential:
        md["token_endpoint_auth_method"] = "client_secret_post"
    return await service.register_client(md)


def _authorize_params(client_id: str, **overrides) -> dict:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    params.update(overrides)
    return params


# --- DCR -------------------------------------------------------------------------------------


async def test_register_public_client(ctx):
    service, store, _ = ctx
    reg = await _register(service)
    assert reg.client_id.startswith("mcp_")
    assert reg.client_secret is None  # public → PKCE, no secret
    assert store.clients[reg.client_id].client_secret_hash is None
    assert reg.metadata["redirect_uris"] == [REDIRECT]
    assert reg.metadata["scope"] == "brain"


async def test_register_confidential_client_issues_secret(ctx):
    service, store, _ = ctx
    reg = await _register(service, confidential=True)
    assert reg.client_secret is not None
    assert store.clients[reg.client_id].client_secret_hash is not None


async def test_register_rejects_empty_redirects(ctx):
    service, _, _ = ctx
    with pytest.raises(InvalidClientMetadata):
        await service.register_client({"redirect_uris": []})


async def test_register_rejects_dangerous_scheme(ctx):
    service, _, _ = ctx
    with pytest.raises(InvalidClientMetadata):
        await service.register_client({"redirect_uris": ["javascript:alert(1)"]})


async def test_register_allows_native_custom_scheme(ctx):
    service, _, _ = ctx
    reg = await service.register_client({"redirect_uris": ["com.example.app://cb"]})
    assert reg.client_id


async def test_register_rejects_non_loopback_http(ctx):
    # OAuth 2.1: non-loopback redirects must use https — plaintext http to a public host would
    # leak the code in the clear (ADR-046 §2 security review).
    service, _, _ = ctx
    with pytest.raises(InvalidClientMetadata):
        await service.register_client({"redirect_uris": ["http://evil.example/cb"]})


async def test_register_allows_loopback_http(ctx):
    # Loopback http stays valid for native/dev clients (nothing on the wire to intercept).
    service, _, _ = ctx
    reg = await service.register_client(
        {"redirect_uris": ["http://127.0.0.1:8765/cb", "http://localhost/cb"]}
    )
    assert reg.client_id


# --- /authorize validation -------------------------------------------------------------------


async def test_authorize_happy(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    req = await service.load_authorization_request(_authorize_params(reg.client_id))
    assert req.redirect_uri == REDIRECT
    assert req.scope == "brain"
    assert req.resource == "https://example.test/mcp"
    assert req.state == "xyz"


async def test_authorize_unknown_client_renders(ctx):
    service, _, _ = ctx
    with pytest.raises(AuthorizeError):
        await service.load_authorization_request(_authorize_params("nope"))


async def test_authorize_bad_redirect_renders(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    with pytest.raises(AuthorizeError):
        await service.load_authorization_request(
            _authorize_params(reg.client_id, redirect_uri="https://evil.test/cb")
        )


async def test_authorize_missing_pkce_redirects(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    params = _authorize_params(reg.client_id)
    del params["code_challenge"]
    with pytest.raises(AuthorizeRedirectError) as exc:
        await service.load_authorization_request(params)
    assert exc.value.error == "invalid_request"
    assert exc.value.state == "xyz"


async def test_authorize_plain_pkce_rejected(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    with pytest.raises(AuthorizeRedirectError) as exc:
        await service.load_authorization_request(
            _authorize_params(reg.client_id, code_challenge_method="plain")
        )
    assert exc.value.error == "invalid_request"


async def test_authorize_bad_response_type_redirects(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    with pytest.raises(AuthorizeRedirectError) as exc:
        await service.load_authorization_request(
            _authorize_params(reg.client_id, response_type="token")
        )
    assert exc.value.error == "unsupported_response_type"


async def test_authorize_unknown_scope_redirects(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    with pytest.raises(AuthorizeRedirectError) as exc:
        await service.load_authorization_request(
            _authorize_params(reg.client_id, scope="brain admin")
        )
    assert exc.value.error == "invalid_scope"


async def test_authorize_wrong_resource_redirects(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    with pytest.raises(AuthorizeRedirectError) as exc:
        await service.load_authorization_request(
            _authorize_params(reg.client_id, resource="https://other.test/mcp")
        )
    assert exc.value.error == "invalid_target"


# --- code exchange ---------------------------------------------------------------------------


async def _issue_code(service, client_id: str) -> str:
    req = await service.load_authorization_request(_authorize_params(client_id))
    return await service.issue_code(req)


async def test_code_exchange_round_trip(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    grant = await service.exchange_code(
        code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    assert grant.access_token and grant.refresh_token
    assert grant.expires_in == 3600
    assert grant.scope == "brain"
    info = await service.validate_access_token(grant.access_token)
    assert info is not None and info.client_id == reg.client_id


async def test_code_exchange_wrong_verifier(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier="b" * 64
        )


async def test_code_exchange_missing_verifier(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=None
        )


async def test_code_single_use_and_replay_revokes(ctx):
    service, store, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    grant = await service.exchange_code(
        code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    # Second exchange of the same code fails AND revokes the client's already-issued tokens.
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )
    assert await service.validate_access_token(grant.access_token) is None


async def test_code_expired(ctx):
    service, _, clock = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    clock.advance(seconds=301)  # past the 300s code TTL
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )


async def test_code_redirect_mismatch(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code,
            client_id=reg.client_id,
            redirect_uri="https://claude.ai/other",
            code_verifier=VERIFIER,
        )


async def test_code_client_mismatch(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    other = await service.register_client({"redirect_uris": [REDIRECT]})
    code = await _issue_code(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=other.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )


async def test_confidential_client_requires_secret(ctx):
    service, _, _ = ctx
    reg = await _register(service, confidential=True)
    code = await _issue_code(service, reg.client_id)
    with pytest.raises(InvalidClient):
        await service.exchange_code(
            code=code,
            client_id=reg.client_id,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
            client_secret=None,
        )


async def test_confidential_client_accepts_secret(ctx):
    service, _, _ = ctx
    reg = await _register(service, confidential=True)
    code = await _issue_code(service, reg.client_id)
    grant = await service.exchange_code(
        code=code,
        client_id=reg.client_id,
        redirect_uri=REDIRECT,
        code_verifier=VERIFIER,
        client_secret=reg.client_secret,
    )
    assert grant.access_token


# --- refresh rotation ------------------------------------------------------------------------


async def _first_grant(service, client_id: str):
    code = await _issue_code(service, client_id)
    return await service.exchange_code(
        code=code, client_id=client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )


async def test_refresh_rotates(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    new = await service.refresh(refresh_token=grant.refresh_token, client_id=reg.client_id)
    assert new.access_token != grant.access_token
    assert new.refresh_token != grant.refresh_token
    # The old refresh is now revoked (rotation) — a fresh access token still validates.
    assert await service.validate_access_token(new.access_token) is not None


async def test_refresh_reuse_detected(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    new = await service.refresh(refresh_token=grant.refresh_token, client_id=reg.client_id)
    # Replaying the rotated-out refresh token → invalid AND revokes the whole client.
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=grant.refresh_token, client_id=reg.client_id)
    assert await service.validate_access_token(new.access_token) is None


async def test_refresh_expired(ctx):
    service, _, clock = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    clock.advance(days=61)  # past the 60-day refresh TTL
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=grant.refresh_token, client_id=reg.client_id)


async def test_refresh_client_mismatch(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    other = await service.register_client({"redirect_uris": [REDIRECT]})
    grant = await _first_grant(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=grant.refresh_token, client_id=other.client_id)


async def test_access_token_not_usable_as_refresh(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=grant.access_token, client_id=reg.client_id)


# --- token validation + revoke-all -----------------------------------------------------------


async def test_validate_rejects_refresh_and_unknown(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    assert await service.validate_access_token(grant.refresh_token) is None
    assert await service.validate_access_token("garbage") is None
    assert await service.validate_access_token("") is None


async def test_validate_rejects_expired_access(ctx):
    service, _, clock = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    clock.advance(seconds=3601)  # past the 1h access TTL
    assert await service.validate_access_token(grant.access_token) is None


async def test_revoke_all(ctx):
    service, _, _ = ctx
    reg = await _register(service)
    g1 = await _first_grant(service, reg.client_id)
    g2 = await _first_grant(service, reg.client_id)
    revoked = await service.revoke_all()
    assert revoked == 4  # 2 access + 2 refresh
    assert await service.validate_access_token(g1.access_token) is None
    assert await service.validate_access_token(g2.access_token) is None
    # A revoked refresh can no longer rotate.
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=g1.refresh_token, client_id=reg.client_id)


async def test_revoke_all_invalidates_pending_code(ctx):
    # Review finding 2: revoke-all must be TOTAL — an issued-but-unexchanged code cannot still mint
    # tokens after the switch is thrown.
    service, _, _ = ctx
    reg = await _register(service)
    code = await _issue_code(service, reg.client_id)
    await service.revoke_all()
    with pytest.raises(InvalidGrant):
        await service.exchange_code(
            code=code, client_id=reg.client_id, redirect_uri=REDIRECT, code_verifier=VERIFIER
        )


async def test_refresh_rotation_is_atomic(ctx):
    # Review finding 1: the revoke is the race-decider. Simulate a concurrent second presentation of
    # the same refresh by pre-revoking it (rowcount 0 path) → reuse detection revokes the client.
    service, store, _ = ctx
    reg = await _register(service)
    grant = await _first_grant(service, reg.client_id)
    other_access = (await _first_grant(service, reg.client_id)).access_token
    # Manually revoke the presented refresh out from under the call (mimics the race loser's state).
    await store.revoke_token(service._hash(grant.refresh_token))
    with pytest.raises(InvalidGrant):
        await service.refresh(refresh_token=grant.refresh_token, client_id=reg.client_id)
    # Reuse detection revoked the whole client, incl. the other live access token.
    assert await service.validate_access_token(other_access) is None


async def test_register_rejects_client_secret_basic(ctx):
    # Review finding 3: basic auth isn't wired at /token, so it must not be registrable.
    service, _, _ = ctx
    with pytest.raises(InvalidClientMetadata):
        await service.register_client(
            {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"}
        )


async def test_token_record_replace_is_frozen(ctx):
    # Guards the frozen dataclass contract the store returns (defensive; cheap).
    rec = TokenRecord(
        id="1",
        client_id="c",
        kind="access",
        scope="brain",
        resource=None,
        expires_at=datetime.now(UTC),
        revoked_at=None,
    )
    assert replace(rec, kind="refresh").kind == "refresh"
