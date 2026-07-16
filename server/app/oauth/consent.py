"""Server-rendered `/authorize` consent + error pages (M5 task 3, ADR-046 §2).

The consent screen is the choke point (ADR-046 §2): explicit Approve/Deny (defends against a
malicious site silently driving the flow in a logged-in browser), a password field when there is
no valid PWA session, a double-submit CSRF token, and the authorization-request parameters carried
forward as hidden fields (re-validated on POST). Every dynamic value is HTML-escaped — the client
name and redirect come from open Dynamic Client Registration and are untrusted input.

Plain string templating (no template-engine dependency); the two functions are pure so they
unit-test without a request.
"""

from __future__ import annotations

from html import escape

# The hidden authorization-request fields carried GET → POST (re-validated server-side on POST).
CARRIED_FIELDS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
)

_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 26rem; margin: 4rem auto; padding: 0 1.25rem; line-height: 1.5; }
  .card { border: 1px solid rgba(128,128,128,.3); border-radius: 14px; padding: 1.5rem 1.75rem; }
  h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
  .sub { color: #888; font-size: .9rem; margin: 0 0 1.25rem; }
  .scope { background: rgba(128,128,128,.12); border-radius: 8px; padding: .6rem .8rem;
           font-size: .9rem; margin: 0 0 1.25rem; }
  label { display: block; font-size: .85rem; margin: 0 0 .3rem; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .6rem .7rem;
           border: 1px solid rgba(128,128,128,.4); border-radius: 8px; margin: 0 0 1.25rem;
           font-size: 1rem; background: transparent; color: inherit; }
  .row { display: flex; gap: .75rem; }
  button { flex: 1; padding: .7rem 1rem; border-radius: 8px; border: 0; font-size: .95rem;
           cursor: pointer; }
  .approve { background: #2563eb; color: #fff; }
  .deny { background: rgba(128,128,128,.18); color: inherit; }
  .err { color: #dc2626; font-size: .85rem; margin: 0 0 1rem; }
  code { word-break: break-all; }
""".strip()


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f'<body><div class="card">{body}</div></body></html>'
    )


def render_error_page(title: str, message: str) -> str:
    """A dead-end error page for an ``/authorize`` failure that must not redirect (untrusted
    ``redirect_uri`` / unknown client)."""
    return _page(
        title,
        f'<h1>{escape(title)}</h1><p class="sub">{escape(message)}</p>',
    )


def render_consent_page(
    *,
    app_name: str,
    client_name: str,
    scope: str,
    needs_password: bool,
    csrf_token: str,
    fields: dict[str, str],
    error: str | None = None,
) -> str:
    """The Approve/Deny consent form. ``fields`` are the carried authorization-request params;
    ``needs_password`` adds the password field when there is no valid PWA session."""
    hidden = "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(fields.get(name, "") or "")}">'
        for name in CARRIED_FIELDS
    )
    hidden += f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'

    error_html = f'<p class="err">{escape(error)}</p>' if error else ""
    password_html = ""
    if needs_password:
        password_html = (
            '<label for="password">Password</label>'
            '<input id="password" type="password" name="password" '
            'autocomplete="current-password" autofocus>'
        )

    body = (
        f"<h1>Connect to {escape(app_name)}</h1>"
        f'<p class="sub"><strong>{escape(client_name)}</strong> is requesting access to your '
        f"second brain.</p>"
        f'<div class="scope">Grants: <strong>{escape(scope)}</strong> — read your graph and '
        f"capture new memories.</div>"
        f"{error_html}"
        '<form method="post" action="/authorize">'
        f"{hidden}"
        f"{password_html}"
        '<div class="row">'
        '<button class="deny" type="submit" name="decision" value="deny">Deny</button>'
        '<button class="approve" type="submit" name="decision" '
        'value="approve">Approve</button>'
        "</div>"
        "</form>"
    )
    return _page(f"Connect to {app_name}", body)
