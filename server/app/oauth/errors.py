"""OAuth 2.1 error taxonomy (RFC 6749 §4.1.2.1 / §5.2, ADR-046 §2).

Two failure shapes reach the client differently, so they are two exception families:

  * :class:`OAuthError` — a protocol error the ``/token`` and ``/register`` endpoints return as a
    JSON ``{error, error_description}`` body with an HTTP status. Also carried back to the client
    on ``/authorize`` **as a redirect** once the ``redirect_uri`` is trusted.
  * :class:`AuthorizeError` — an ``/authorize`` error raised **before** the ``redirect_uri`` can be
    trusted (unknown client, bad/blank redirect). These must NOT redirect (open-redirect / phishing
    risk) — the router renders a server-side error page instead.

The ``error`` slugs are the RFC-registered codes so a compliant connector reacts correctly.
"""

from __future__ import annotations


class OAuthError(Exception):
    """A redirect-safe / body-safe OAuth error (RFC 6749). ``status`` = its ``/token`` HTTP code."""

    error: str = "invalid_request"
    status: int = 400

    def __init__(
        self, description: str = "", *, error: str | None = None, status: int | None = None
    ) -> None:
        if error is not None:
            self.error = error
        if status is not None:
            self.status = status
        self.description = description
        super().__init__(f"{self.error}: {description}" if description else self.error)

    def to_dict(self) -> dict[str, str]:
        body = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        return body


class InvalidRequest(OAuthError):
    error = "invalid_request"
    status = 400


class InvalidClient(OAuthError):
    error = "invalid_client"
    status = 401


class InvalidGrant(OAuthError):
    error = "invalid_grant"
    status = 400


class UnsupportedGrantType(OAuthError):
    error = "unsupported_grant_type"
    status = 400


class InvalidScope(OAuthError):
    error = "invalid_scope"
    status = 400


class InvalidTarget(OAuthError):
    # RFC 8707 — the requested `resource` is not one this server serves.
    error = "invalid_target"
    status = 400


class AccessDenied(OAuthError):
    error = "access_denied"
    status = 403


class InvalidClientMetadata(OAuthError):
    # RFC 7591 §3.2.2 — the Dynamic Client Registration request was malformed.
    error = "invalid_client_metadata"
    status = 400


class AuthorizeError(Exception):
    """An ``/authorize`` error that must render a page, never redirect (untrusted redirect_uri)."""

    def __init__(self, title: str, message: str) -> None:
        self.title = title
        self.message = message
        super().__init__(f"{title}: {message}")


class AuthorizeRedirectError(Exception):
    """An ``/authorize`` error raised **after** the ``redirect_uri`` is validated — carried back to
    the client as a redirect with ``error``/``state`` query params (RFC 6749 §4.1.2.1)."""

    def __init__(
        self, *, redirect_uri: str, error: str, description: str = "", state: str | None = None
    ) -> None:
        self.redirect_uri = redirect_uri
        self.error = error
        self.description = description
        self.state = state
        super().__init__(f"{error}: {description}" if description else error)
