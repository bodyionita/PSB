"""OAuth 2.1 authorization-server flow (M5 task 3, ADR-046 §2).

The orchestration behind the four endpoints — open DCR, the `/authorize` choke point, `/token`
(code exchange + refresh rotation), and the resource-server token check `/mcp` (task 4) reuses.
Security-critical crypto is authlib's: PKCE S256 (`create_s256_code_challenge`) and secure token
generation (`generate_token`). Everything a connector receives (codes, tokens) leaves the server
once as plaintext; only HMAC hashes are stored (same discipline as web sessions).

Depends on the :class:`OAuthStore` protocol + :class:`AuthService`, so it unit-tests against fakes
with no DB (08 testing policy).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from ..config import Settings
from ..security import hash_session_token
from .errors import (
    AuthorizeError,
    AuthorizeRedirectError,
    InvalidClient,
    InvalidClientMetadata,
    InvalidGrant,
    InvalidRequest,
)
from .metadata import mcp_resource_id
from .store import ClientRecord, OAuthStore

# Schemes an open-DCR client may NOT register a redirect to (script/data-exfil vectors). Everything
# else — https, http (localhost dev), and native custom app schemes — is allowed (MCP native apps).
_FORBIDDEN_REDIRECT_SCHEMES = frozenset({"javascript", "data", "file", "vbscript"})
_SUPPORTED_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})
# RFC 7636 §4.1 — the code verifier is 43–128 chars from the unreserved set.
_PKCE_VERIFIER_MIN, _PKCE_VERIFIER_MAX = 43, 128


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    client_id_issued_at: int
    client_secret: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AuthorizationRequest:
    """A fully validated ``/authorize`` request — client trusted, ``redirect_uri`` matched, PKCE +
    scope + resource checked. Carried into the consent render and the code issue."""

    client: ClientRecord
    redirect_uri: str
    scope: str
    state: str | None
    code_challenge: str
    code_challenge_method: str
    resource: str

    @property
    def client_name(self) -> str:
        name = self.client.metadata.get("client_name")
        return name if isinstance(name, str) and name.strip() else self.client.client_id

    def carried_fields(self) -> dict[str, str]:
        return {
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": self.state or "",
            "code_challenge": self.code_challenge,
            "code_challenge_method": self.code_challenge_method,
            "resource": self.resource,
        }


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class AccessTokenInfo:
    """The resource-server view of a valid access token (task 4's ``/mcp`` auth uses this)."""

    client_id: str
    scope: str
    resource: str | None = field(default=None)


class OAuthService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: OAuthStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._now = clock or (lambda: datetime.now(UTC))
        self._scope = settings.mcp_oauth_scope
        self._resource = mcp_resource_id(settings)

    # --- Dynamic Client Registration (RFC 7591, open) ----------------------------------------

    async def register_client(self, metadata: Mapping[str, Any]) -> RegisteredClient:
        """Register a connector from its submitted metadata. Inert on its own — a client grants
        nothing until it completes the ``/authorize`` flow (ADR-046 §2)."""
        redirect_uris = metadata.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise InvalidClientMetadata("redirect_uris must be a non-empty array")
        for uri in redirect_uris:
            if not isinstance(uri, str) or not _is_allowed_redirect(uri):
                raise InvalidClientMetadata(f"invalid redirect_uri: {uri!r}")

        grant_types = metadata.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grant_types, list) or set(grant_types) - _SUPPORTED_GRANT_TYPES:
            raise InvalidClientMetadata("unsupported grant_types")

        auth_method = metadata.get("token_endpoint_auth_method", "none")
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            raise InvalidClientMetadata("unsupported token_endpoint_auth_method")

        client_id = "mcp_" + generate_token(24)
        client_secret: str | None = None
        secret_hash: str | None = None
        if auth_method != "none":
            client_secret = generate_token(48)
            secret_hash = self._hash(client_secret)

        stored = {
            "redirect_uris": list(redirect_uris),
            "client_name": _clean_str(metadata.get("client_name")),
            "token_endpoint_auth_method": auth_method,
            "grant_types": list(grant_types),
            "response_types": ["code"],
            "scope": self._scope,
        }
        await self._store.create_client(
            client_id=client_id, client_secret_hash=secret_hash, metadata=stored
        )
        return RegisteredClient(
            client_id=client_id,
            client_id_issued_at=int(self._now().timestamp()),
            client_secret=client_secret,
            metadata=stored,
        )

    # --- /authorize --------------------------------------------------------------------------

    async def load_authorization_request(self, params: Mapping[str, str]) -> AuthorizationRequest:
        """Validate an incoming ``/authorize`` request. Raises :class:`AuthorizeError` when the
        error must render a page (untrusted client/redirect) and :class:`AuthorizeRedirectError`
        once ``redirect_uri`` is trusted (carried back as a redirect). Shared by GET (consent) and
        POST (before issuing a code) so both apply the identical checks."""
        client_id = (params.get("client_id") or "").strip()
        if not client_id:
            raise AuthorizeError("Invalid request", "Missing client_id.")
        client = await self._store.get_client(client_id)
        if client is None:
            raise AuthorizeError("Unknown client", "This client is not registered.")

        registered = client.metadata.get("redirect_uris") or []
        redirect_uri = (params.get("redirect_uri") or "").strip()
        # Exact match against the registered set (OAuth 2.1 — no prefix/substring matching). Until
        # this holds we must NOT redirect anywhere (open-redirect / token-phishing guard).
        if not redirect_uri or redirect_uri not in registered:
            raise AuthorizeError(
                "Invalid redirect", "The redirect_uri does not match this client's registration."
            )

        state = params.get("state") or None
        # Everything below is trusted-redirect territory → errors go back as a redirect.
        response_type = (params.get("response_type") or "").strip()
        if response_type != "code":
            raise AuthorizeRedirectError(
                redirect_uri=redirect_uri,
                error="unsupported_response_type",
                description="only response_type=code is supported",
                state=state,
            )

        code_challenge = (params.get("code_challenge") or "").strip()
        if not code_challenge:
            raise AuthorizeRedirectError(
                redirect_uri=redirect_uri,
                error="invalid_request",
                description="PKCE code_challenge is required",
                state=state,
            )
        method = (params.get("code_challenge_method") or "S256").strip()
        if method != "S256":
            raise AuthorizeRedirectError(
                redirect_uri=redirect_uri,
                error="invalid_request",
                description="only PKCE code_challenge_method=S256 is supported",
                state=state,
            )

        scope = self._validate_scope(params.get("scope"), redirect_uri=redirect_uri, state=state)
        resource = self._validate_resource(
            params.get("resource"), redirect_uri=redirect_uri, state=state
        )
        return AuthorizationRequest(
            client=client,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=method,
            resource=resource,
        )

    async def issue_code(self, req: AuthorizationRequest) -> str:
        """Mint a single-use, PKCE-bound authorization code (plaintext once, only its hash kept)."""
        code = generate_token(48)
        expires_at = self._now() + timedelta(seconds=self._settings.mcp_auth_code_ttl_seconds)
        await self._store.create_code(
            code_hash=self._hash(code),
            client_id=req.client.client_id,
            redirect_uri=req.redirect_uri,
            code_challenge=req.code_challenge,
            code_challenge_method=req.code_challenge_method,
            scope=req.scope,
            resource=req.resource,
            expires_at=expires_at,
        )
        return code

    # --- /token: authorization_code grant ----------------------------------------------------

    async def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str | None,
        client_secret: str | None = None,
    ) -> TokenGrant:
        if not code:
            raise InvalidRequest("missing code")
        record = await self._store.consume_code(self._hash(code))
        if record is None:
            # Miss: replayed already-spent code → revoke that client's tokens (replay guard).
            owner = await self._store.consumed_code_client(self._hash(code))
            if owner is not None:
                await self._store.revoke_client_tokens(owner)
            raise InvalidGrant("authorization code is invalid, expired, or already used")

        if record.client_id != client_id:
            raise InvalidGrant("authorization code was issued to a different client")
        if record.redirect_uri != redirect_uri:
            raise InvalidGrant("redirect_uri does not match the authorization request")

        client = await self._require_client(client_id)
        self._authenticate_client(client, client_secret)
        self._verify_pkce(record.code_challenge, code_verifier)

        return await self._issue_grant(client_id, record.scope, record.resource)

    # --- /token: refresh_token grant (rotation) ----------------------------------------------

    async def refresh(
        self, *, refresh_token: str, client_id: str, client_secret: str | None = None
    ) -> TokenGrant:
        if not refresh_token:
            raise InvalidRequest("missing refresh_token")
        record = await self._store.get_token(self._hash(refresh_token))
        if record is None or record.kind != "refresh":
            raise InvalidGrant("refresh token is invalid")
        if record.revoked_at is not None:
            # A revoked refresh token being replayed ⇒ likely a rotated-token leak. Revoke the whole
            # client (OAuth 2.1 refresh-rotation reuse detection).
            await self._store.revoke_client_tokens(record.client_id)
            raise InvalidGrant("refresh token has been revoked")
        if record.expires_at <= self._now():
            raise InvalidGrant("refresh token has expired")
        if record.client_id != client_id:
            raise InvalidGrant("refresh token was issued to a different client")

        client = await self._require_client(client_id)
        self._authenticate_client(client, client_secret)

        # Rotate: revoke the presented refresh, mint a fresh access+refresh pair (sliding lifetime).
        await self._store.revoke_token(self._hash(refresh_token))
        return await self._issue_grant(client_id, record.scope, record.resource)

    # --- resource-server token validation (task 4 reuses) ------------------------------------

    async def validate_access_token(self, token: str) -> AccessTokenInfo | None:
        """Validate a bearer access token for the ``/mcp`` resource. ``None`` when missing, not an
        access token, revoked, or expired — never raises (a gate must fail closed, not crash)."""
        if not token:
            return None
        record = await self._store.get_token(self._hash(token))
        if record is None or record.kind != "access":
            return None
        if record.revoked_at is not None or record.expires_at <= self._now():
            return None
        return AccessTokenInfo(
            client_id=record.client_id, scope=record.scope, resource=record.resource
        )

    # --- revoke-all switch -------------------------------------------------------------------

    async def revoke_all(self) -> int:
        """The M5 "revoke all MCP access" control — flag every live token (ADR-046 §2)."""
        return await self._store.revoke_all()

    # --- internals ---------------------------------------------------------------------------

    async def _issue_grant(self, client_id: str, scope: str, resource: str | None) -> TokenGrant:
        now = self._now()
        access = generate_token(48)
        refresh = generate_token(48)
        await self._store.create_token(
            client_id=client_id,
            token_hash=self._hash(access),
            kind="access",
            scope=scope,
            resource=resource,
            expires_at=now + timedelta(seconds=self._settings.mcp_access_token_ttl_seconds),
        )
        await self._store.create_token(
            client_id=client_id,
            token_hash=self._hash(refresh),
            kind="refresh",
            scope=scope,
            resource=resource,
            expires_at=now + timedelta(days=self._settings.mcp_refresh_token_ttl_days),
        )
        return TokenGrant(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._settings.mcp_access_token_ttl_seconds,
            scope=scope,
        )

    async def _require_client(self, client_id: str) -> ClientRecord:
        client = await self._store.get_client(client_id)
        if client is None:
            raise InvalidClient("unknown client")
        return client

    def _authenticate_client(self, client: ClientRecord, client_secret: str | None) -> None:
        """Public (PKCE) clients need no secret; a confidential client's posted secret is verified
        against the stored hash in constant time."""
        if client.client_secret_hash is None:
            return  # public client — PKCE is the proof of possession
        if not client_secret or not hmac.compare_digest(
            self._hash(client_secret), client.client_secret_hash
        ):
            raise InvalidClient("invalid client credentials")

    def _verify_pkce(self, code_challenge: str, code_verifier: str | None) -> None:
        if not code_verifier:
            raise InvalidGrant("PKCE code_verifier is required")
        if not _PKCE_VERIFIER_MIN <= len(code_verifier) <= _PKCE_VERIFIER_MAX:
            raise InvalidGrant("PKCE code_verifier has an invalid length")
        expected = create_s256_code_challenge(code_verifier)
        if not hmac.compare_digest(expected, code_challenge):
            raise InvalidGrant("PKCE verification failed")

    def _validate_scope(
        self, requested: str | None, *, redirect_uri: str, state: str | None
    ) -> str:
        # M5 grants the single full-access scope. An empty request defaults to it; a request naming
        # an unknown scope is rejected (redirected as invalid_scope).
        tokens = (requested or "").split()
        if any(tok != self._scope for tok in tokens):
            raise AuthorizeRedirectError(
                redirect_uri=redirect_uri,
                error="invalid_scope",
                description=f"unknown scope; only '{self._scope}' is supported",
                state=state,
            )
        return self._scope

    def _validate_resource(
        self, requested: str | None, *, redirect_uri: str, state: str | None
    ) -> str:
        # RFC 8707: if the client names a resource it must be this server's MCP endpoint; absent
        # defaults to it. Bound onto the code + tokens as the audience.
        if requested and requested.rstrip("/") != self._resource.rstrip("/"):
            raise AuthorizeRedirectError(
                redirect_uri=redirect_uri,
                error="invalid_target",
                description="unknown resource",
                state=state,
            )
        return self._resource

    def _hash(self, value: str) -> str:
        return hash_session_token(value, self._settings.mcp_token_hmac_secret)


def _is_allowed_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if not parts.scheme or parts.scheme.lower() in _FORBIDDEN_REDIRECT_SCHEMES:
        return False
    # An http/https redirect needs a host; a native custom scheme (com.app://cb) needs a body.
    if parts.scheme.lower() in ("http", "https"):
        return bool(parts.netloc)
    return bool(parts.netloc or parts.path)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
