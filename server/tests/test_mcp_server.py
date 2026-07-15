"""MCP protocol integration harness (M5 task 4, ADR-046 §3) — an in-memory MCP-SDK client drives
the real FastMCP server over the protocol (initialize → list/call tools → read resource → get
prompt), with the service layer faked. Verifies the wiring, annotations, and Markdown boundary
without HTTP or a DB (auth is exercised separately at the OAuth layer)."""

from __future__ import annotations

from types import SimpleNamespace

from mcp.shared.memory import create_connected_server_and_client_session

from app.config import Settings
from app.graph.service import ContextNeighbor, NeighborPage, NodeContext
from app.graph.store import NeighborEdge
from app.identity.store import CapsuleBlob
from app.mcp.server import build_mcp_server
from app.mcp.text import SERVER_INSTRUCTIONS
from app.search.service import NodePreview
from app.search.store import NodeEdgeView, SearchHit

NID = "11111111-1111-1111-1111-111111111111"
NID2 = "22222222-2222-2222-2222-222222222222"


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, query, **kw):
        self.calls.append({"query": query, **kw})
        return [
            SearchHit(
                node_id=NID, store_path="p", type="memory", title="Pricing call",
                plane="Professional", planes=["Professional"], tags=["pricing"],
                snippet="We raised prices.", score=0.03,
            )
        ]

    async def get_node(self, node_id):
        if node_id != NID:
            return None
        return NodePreview(
            node_id=NID, store_path="p", type="memory", title="Pricing call", plane="Professional",
            planes=["Professional"], tags=["pricing"], aliases=[], disambig=None, occurred=None,
            occurred_end=None, body="We raised prices.", profile=None,
            edges=[NodeEdgeView(rel="involves", dir="out", node_id=NID2, type="person",
                                title="Alex", origin="canonical", score=None, since=None,
                                until=None)],
            merged_into=None,
        )


class FakeGraph:
    async def neighbors(self, node_id, *, rel=None, direction="both", cursor=None):
        edge = NeighborEdge(origin="canonical", rel="involves", dir="out", node_id=NID2,
                            type="person", title="Alex", plane="Professional", score=None,
                            since=None, until=None)
        return NeighborPage(center_id=node_id, neighbors=[edge], next_cursor="CUR", rel=rel,
                            direction=direction)

    async def build_context(self, node_id, *, depth=None):
        if node_id != NID:
            return None
        node = NodePreview(
            node_id=NID, store_path="p", type="memory", title="Pricing call", plane="Professional",
            planes=["Professional"], tags=[], aliases=[], disambig=None, occurred=None,
            occurred_end=None, body="body", profile=None, edges=[], merged_into=None,
        )
        edge = NeighborEdge(origin="canonical", rel="involves", dir="out", node_id=NID2,
                            type="person", title="Alex", plane=None, score=None, since=None,
                            until=None)
        return NodeContext(node=node, neighbors=[ContextNeighbor(edge=edge)], depth=1,
                           truncated=False, identity_capsule="The user runs a startup.")


class FakeVocab:
    async def list_types(self):
        return SimpleNamespace(node_types=["memory", "person"], edge_rels=["involves"],
                               entity_like_types=["person"], proposals=[])


class FakeCapture:
    def __init__(self) -> None:
        self.captured: list[str] = []

    async def create_mcp_capture(self, text):
        self.captured.append(text)
        return "cap-123"


class FakeCapsuleStore:
    async def current(self):
        return CapsuleBlob(text="I build things.")


def _mcp():
    settings = Settings(public_base_url="https://x.test", mcp_token_hmac_secret="x")
    search, graph, capture = FakeSearch(), FakeGraph(), FakeCapture()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings, oauth_service=None, search_service=search, graph_service=graph,
            capture_pipeline=capture, vocabulary_service=FakeVocab(),
            identity_capsule_store=FakeCapsuleStore(),
        )
    )
    return build_mcp_server(app, settings), search, capture


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")


async def test_instructions_capsule_present():
    mcp, _, _ = _mcp()
    assert mcp.instructions == SERVER_INSTRUCTIONS


async def test_list_tools_and_annotations():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "search", "get_node", "traverse", "build_context", "list_planes", "list_types", "capture"
    }
    assert by_name["search"].annotations.readOnlyHint is True
    assert by_name["capture"].annotations.readOnlyHint is False  # the write tool
    assert by_name["search"].description  # rich description present


async def test_call_search_and_temporal_filter():
    mcp, search, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        res = await client.call_tool("search", {"query": "pricing", "since": "2026-01-01"})
    md = _text(res)
    assert "Pricing call" in md and "`" + NID + "`" in md
    assert search.calls[0]["query"] == "pricing"
    assert str(search.calls[0]["since"]) == "2026-01-01"  # parsed to a date


async def test_call_search_bad_date():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        res = await client.call_tool("search", {"query": "x", "since": "nope"})
    assert "ISO date" in _text(res)


async def test_call_get_node_and_missing():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        ok = await client.call_tool("get_node", {"id": NID})
        missing = await client.call_tool("get_node", {"id": "nope"})
    assert "Pricing call" in _text(ok) and NID in _text(ok)
    assert "No node with id" in _text(missing)


async def test_call_traverse_and_build_context():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        trav = _text(await client.call_tool("traverse", {"id": NID}))
        ctx = _text(await client.call_tool("build_context", {"id": NID}))
    assert "involves" in trav and 'cursor="CUR"' in trav
    assert "The user runs a startup." in ctx and "Context (depth 1)" in ctx


async def test_call_capture_writes():
    mcp, _, capture = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        res = _text(await client.call_tool("capture", {"text": "Met Alex about pricing."}))
    assert "`cap-123`" in res
    assert capture.captured == ["Met Alex about pricing."]


async def test_list_planes_and_types():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        planes = _text(await client.call_tool("list_planes", {}))
        types = _text(await client.call_tool("list_types", {}))
    assert "Professional" in planes
    assert "memory" in types and "involves" in types


async def test_identity_resource():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        resources = (await client.list_resources()).resources
        assert any(str(r.uri) == "identity://me" for r in resources)
        read = await client.read_resource("identity://me")
    assert "I build things." in read.contents[0].text


async def test_research_prompt():
    mcp, _, _ = _mcp()
    async with create_connected_server_and_client_session(mcp) as client:
        prompts = (await client.list_prompts()).prompts
        assert any(p.name == "research" for p in prompts)
        got = await client.get_prompt("research", {"topic": "pricing strategy"})
    text = " ".join(m.content.text for m in got.messages)
    assert "pricing strategy" in text and "capture" in text


async def test_traverse_maps_invalid_direction():
    # A GraphService.neighbors that raises InvalidDirection is mapped to a helpful message, not an
    # error result (the tool reads graph_service lazily, so we can swap in a raising fake).
    from app.graph.service import InvalidDirection

    settings = Settings(public_base_url="https://x.test", mcp_token_hmac_secret="x")

    class RaisingGraph:
        async def neighbors(self, node_id, *, rel=None, direction="both", cursor=None):
            raise InvalidDirection(direction)

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings, oauth_service=None, search_service=FakeSearch(),
            graph_service=RaisingGraph(), capture_pipeline=FakeCapture(),
            vocabulary_service=FakeVocab(), identity_capsule_store=FakeCapsuleStore(),
        )
    )
    mcp = build_mcp_server(app, settings)
    async with create_connected_server_and_client_session(mcp) as client:
        res = _text(await client.call_tool("traverse", {"id": NID, "direction": "sideways"}))
    assert "out, in, both" in res
