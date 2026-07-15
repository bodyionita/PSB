"""OAuth 2.1 / MCP discovery metadata (M5 task 3, ADR-046 §2).

Two `.well-known` documents let a connector self-configure with no manual token:

  * RFC 8414 **authorization-server metadata** — issuer + endpoint URLs + supported
    grants/PKCE/auth methods. Validated with authlib's schema model so we can't advertise a
    malformed document.
  * RFC 9728 **protected-resource metadata** — points the MCP resource (`<base>/mcp`, RFC 8707)
    at this authorization server.

Everything is derived from ``settings.public_base_url`` (no hardcoded host, rule 9).
"""

from __future__ import annotations

from authlib.oauth2.rfc8414 import AuthorizationServerMetadata

from ..config import Settings

# The MCP resource identifier is the MCP endpoint URL (RFC 8707); token audiences bind to it.
MCP_RESOURCE_PATH = "/mcp"


def _base(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/")


def mcp_resource_id(settings: Settings) -> str:
    """The RFC 8707 resource identifier a token is minted for — ``<public_base_url>/mcp``."""
    return _base(settings) + MCP_RESOURCE_PATH


def authorization_server_metadata(settings: Settings) -> dict[str, object]:
    base = _base(settings)
    doc = {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # OAuth 2.1: only PKCE S256 (never `plain`).
        "code_challenge_methods_supported": ["S256"],
        # Public (PKCE) clients authenticate with no secret; a confidential client may post one.
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": [settings.mcp_oauth_scope],
    }
    # authlib validates the RFC 8414 shape (issuer well-formed, endpoints present, S256 listed).
    AuthorizationServerMetadata(doc).validate()
    return doc


def protected_resource_metadata(settings: Settings) -> dict[str, object]:
    base = _base(settings)
    return {
        "resource": mcp_resource_id(settings),
        "authorization_servers": [base],
        "scopes_supported": [settings.mcp_oauth_scope],
        "bearer_methods_supported": ["header"],
        "resource_name": f"{settings.app_name} MCP",
    }
