"""FastMCP server factory (M5 task 4, ADR-046 §1/§3/§4).

Builds the remote MCP server: the six tools thin over the service layer, the `identity://me`
resource, and the research-via-MCP prompt. Tools read the services from ``app.state`` **lazily**
(the server is built in ``create_app`` but the services are wired in the lifespan), render Markdown
at the boundary, and never hold logic of their own. Auth is the task-3 OAuth resource server via
:class:`OAuthTokenVerifier`; the transport is Streamable HTTP mounted under ``/mcp`` by the caller.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from fastapi import FastAPI
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from ..config import Settings
from ..graph.service import InvalidCursor, InvalidDirection
from ..oauth.metadata import mcp_resource_id
from ..providers.base import ProviderUnavailable
from .render import (
    render_build_context,
    render_capture_ack,
    render_identity_capsule,
    render_node,
    render_planes,
    render_search_results,
    render_traverse,
    render_types,
)
from .text import (
    BUILD_CONTEXT_DESCRIPTION,
    CAPTURE_DESCRIPTION,
    GET_NODE_DESCRIPTION,
    LIST_PLANES_DESCRIPTION,
    LIST_TYPES_DESCRIPTION,
    RESEARCH_PROMPT_DESCRIPTION,
    RESEARCH_PROMPT_NAME,
    SEARCH_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    TRAVERSE_DESCRIPTION,
    research_prompt,
)
from .tokens import OAuthTokenVerifier

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# capture writes into the graph (organizer-owned, non-destructive: it only ever adds).
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


class _BadDate(ValueError):
    pass


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _BadDate(f"`{field}` must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def build_mcp_server(app: FastAPI, settings: Settings) -> FastMCP:
    edge_cap = settings.mcp_inline_edge_cap

    # DNS-rebinding protection (SDK default): FastMCP auto-allows only localhost, so behind the
    # Caddy/Cloudflare edge the real `Host: <domain>` header is rejected with 421 Misdirected
    # Request *after* a successful OAuth handshake. Allow the public host+origin derived from
    # public_base_url (rule 9 — no hardcoded host); the `:*` port wildcards tolerate an explicit
    # port on the Host/Origin. Protection stays ON (the edge fixes Host, but defense-in-depth).
    _netloc = urlparse(settings.public_base_url).netloc  # e.g. braindan.cc / localhost:8000
    _origin = settings.public_base_url.rstrip("/")
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_netloc, f"{_netloc}:*"],
        allowed_origins=[_origin, f"{_origin}:*"],
    )

    mcp = FastMCP(
        name=f"{settings.app_name} Brain",
        instructions=SERVER_INSTRUCTIONS,
        token_verifier=OAuthTokenVerifier(lambda: app.state.oauth_service),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.public_base_url.rstrip("/")),
            resource_server_url=AnyHttpUrl(mcp_resource_id(settings)),
            required_scopes=[settings.mcp_oauth_scope],
        ),
        transport_security=transport_security,
        stateless_http=True,
    )

    # --- tools (thin over the service layer; Markdown at the boundary) -----------------------

    async def search(
        query: str,
        top_k: int | None = None,
        planes: list[str] | None = None,
        types: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        as_of: str | None = None,
    ) -> str:
        try:
            hits = await app.state.search_service.search(
                query,
                top_k=top_k,
                planes=planes,
                types=types,
                since=_parse_date(since, "since"),
                until=_parse_date(until, "until"),
                as_of=_parse_date(as_of, "as_of"),
            )
        except _BadDate as exc:
            return str(exc)
        except ProviderUnavailable:
            return "Search is temporarily unavailable (the embedding provider is down). Try again."
        return render_search_results(query, hits)

    async def get_node(id: str) -> str:
        node = await app.state.search_service.get_node(id)
        if node is None:
            return f"No node with id `{id}`."
        return render_node(node, edge_cap=edge_cap)

    async def traverse(
        id: str, rel: str | None = None, direction: str = "both", cursor: str | None = None
    ) -> str:
        try:
            page = await app.state.graph_service.neighbors(
                id, rel=rel, direction=direction, cursor=cursor
            )
        except InvalidDirection:
            return "`direction` must be one of: out, in, both."
        except InvalidCursor:
            return "That `cursor` is invalid — omit it to start from the first page."
        return render_traverse(page)

    async def build_context(id: str, depth: int | None = None) -> str:
        ctx = await app.state.graph_service.build_context(id, depth=depth)
        if ctx is None:
            return f"No node with id `{id}`."
        return render_build_context(ctx, edge_cap=edge_cap)

    async def list_planes() -> str:
        return render_planes(list(settings.planes), settings.inbox_folder)

    async def list_types() -> str:
        view = await app.state.vocabulary_service.list_types()
        return render_types(
            list(view.node_types), list(view.edge_rels), list(view.entity_like_types)
        )

    async def capture(text: str) -> str:
        if not text or not text.strip():
            return "Nothing to capture — provide some text."
        capture_id = await app.state.capture_pipeline.create_mcp_capture(text)
        return render_capture_ack(capture_id)

    mcp.add_tool(search, description=SEARCH_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(get_node, description=GET_NODE_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(traverse, description=TRAVERSE_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(build_context, description=BUILD_CONTEXT_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(list_planes, description=LIST_PLANES_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(list_types, description=LIST_TYPES_DESCRIPTION, annotations=_READ_ONLY,
                 structured_output=False)
    mcp.add_tool(capture, description=CAPTURE_DESCRIPTION, annotations=_WRITE,
                 structured_output=False)

    # --- resource: the derived identity capsule, up-front, no node needed (ADR-046 §5) ------

    @mcp.resource(
        "identity://me", name="identity", description="Who the user is (identity capsule)"
    )
    async def identity_me() -> str:
        blob = await app.state.identity_capsule_store.current()
        return render_identity_capsule(blob.text if blob else None)

    # --- prompt: research-via-MCP (ADR-033 #6) ----------------------------------------------

    @mcp.prompt(name=RESEARCH_PROMPT_NAME, description=RESEARCH_PROMPT_DESCRIPTION)
    def research(topic: str) -> str:
        return research_prompt(topic)

    return mcp
