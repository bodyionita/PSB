"""Resource-server bearer-token verification for the MCP transport (M5 task 4, ADR-046 §2/§3).

Bridges the task-3 :class:`OAuthService` (which owns the opaque HMAC-hashed token store) to the MCP
SDK's ``TokenVerifier`` protocol. FastMCP's auth middleware calls :meth:`verify_token` on every
request to ``/mcp``; a ``None`` return is a 401 (with a ``WWW-Authenticate`` pointing at our
protected-resource metadata). No new auth logic lives here — it delegates to
``OAuthService.validate_access_token``, which fails closed.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.auth.provider import AccessToken

from ..oauth.service import OAuthService


class OAuthTokenVerifier:
    """Adapts our OAuth resource-server check to the MCP SDK ``TokenVerifier`` protocol.

    Takes a *getter* for the :class:`OAuthService` (not the instance) because the MCP server is
    constructed in ``create_app`` while ``oauth_service`` is wired onto ``app.state`` later in the
    lifespan — the getter resolves it lazily on the first request."""

    def __init__(self, get_oauth: Callable[[], OAuthService]) -> None:
        self._get_oauth = get_oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        info = await self._get_oauth().validate_access_token(token)
        if info is None:
            return None
        return AccessToken(
            token=token,
            client_id=info.client_id,
            scopes=info.scope.split() if info.scope else [],
            resource=info.resource,
        )
