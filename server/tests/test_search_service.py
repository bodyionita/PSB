"""SearchService tests: fake store + fake embedder + tmp store (no DB, no live LLM)."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.providers.registry import ProviderRegistry
from app.search.service import SearchService
from app.search.store import NodeEdgeView, NodeRow, SearchHit

from .fakes import FakeEmbeddingProvider, FakeSearchStore


def _make_service(
    tmp_path: Path,
    *,
    store: FakeSearchStore | None = None,
    embedder: FakeEmbeddingProvider | None = None,
    snippet_max: int = 400,
) -> tuple[SearchService, FakeSearchStore, FakeEmbeddingProvider, Path]:
    root = tmp_path / "store"
    root.mkdir(exist_ok=True)
    store = store or FakeSearchStore()
    embedder = embedder or FakeEmbeddingProvider(dim=4)
    settings = Settings(graph_store_path=str(root), search_snippet_max_chars=snippet_max)
    registry = ProviderRegistry(
        {"fake-embed": embedder},
        chat_chain=[],
        distill_chain=[],
        embedding_provider_id="fake-embed",
        stt_chain=[],
    )
    return SearchService(settings=settings, store=store, registry=registry), store, embedder, root


def _hit(node_id: str = "n1", snippet: str = "a snippet", score: float = 0.9) -> SearchHit:
    return SearchHit(
        node_id=node_id,
        store_path="memory/x.md",
        type="memory",
        title="X",
        plane="Ideas",
        planes=["Ideas"],
        tags=["t"],
        snippet=snippet,
        score=score,
    )


async def test_search_embeds_query_with_search_query_prefix(tmp_path: Path):
    service, store, embedder, _ = _make_service(tmp_path, store=FakeSearchStore(hits=[_hit()]))
    hits = await service.search("what did I decide about pricing")

    prefixed = "search_query: what did I decide about pricing"
    assert embedder.inputs == [[prefixed]]
    assert [h.node_id for h in hits] == ["n1"]
    assert store.search_args["embedding"] == [float(len(prefixed))] * 4


async def test_search_clamps_top_k_to_configured_max(tmp_path: Path):
    service, store, _, _ = _make_service(tmp_path)
    await service.search("q", top_k=9999)
    assert store.search_args["top_k"] == 50  # SEARCH_MAX_TOP_K default

    await service.search("q", top_k=None)
    assert store.search_args["top_k"] == 10  # SEARCH_TOP_K_DEFAULT


async def test_search_empty_filters_become_no_filter(tmp_path: Path):
    service, store, _, _ = _make_service(tmp_path)
    await service.search("q", planes=[], types=[])
    assert store.search_args["planes"] is None
    assert store.search_args["types"] is None

    await service.search("q", planes=["Ideas"], types=["person", "memory"])
    assert store.search_args["planes"] == ["Ideas"]
    assert store.search_args["types"] == ["person", "memory"]


async def test_search_trims_long_snippet(tmp_path: Path):
    long_snippet = "word " * 200  # 1000 chars
    service, _, _, _ = _make_service(
        tmp_path, store=FakeSearchStore(hits=[_hit(snippet=long_snippet)]), snippet_max=50
    )
    hits = await service.search("q")
    assert len(hits[0].snippet) <= 51  # 50 + the ellipsis
    assert hits[0].snippet.endswith("…")


async def test_get_node_reads_body_from_store_and_attaches_edges(tmp_path: Path):
    node = NodeRow(
        node_id="n1",
        store_path="memory/x.md",
        type="memory",
        title="X",
        plane="Ideas",
        planes=["Ideas"],
        tags=["t"],
        aliases=[],
        disambig=None,
        occurred_start=None,
        occurred_end=None,
        merged_into=None,
        edges=[
            NodeEdgeView(
                rel="involves",
                dir="out",
                node_id="p1",
                type="person",
                title="Alex",
                origin="canonical",
                score=None,
                since=None,
                until=None,
            )
        ],
    )
    service, _, _, root = _make_service(tmp_path, store=FakeSearchStore(node=node))
    (root / "memory").mkdir(parents=True)
    (root / "memory" / "x.md").write_text(
        "---\ntype: memory\ntags: [t]\n---\n\n# X\n\nThe living body of X.\n", encoding="utf-8"
    )

    preview = await service.get_node("n1")

    assert preview is not None
    assert preview.body == "# X\n\nThe living body of X."  # frontmatter stripped, content kept
    assert preview.profile is None  # derived profile job lands in task 6
    assert [e.node_id for e in preview.edges] == ["p1"]
    assert preview.merged_into is None


async def test_get_node_unknown_returns_none(tmp_path: Path):
    service, _, _, _ = _make_service(tmp_path, store=FakeSearchStore(node=None))
    assert await service.get_node("missing") is None


async def test_get_node_missing_file_yields_empty_body(tmp_path: Path):
    node = NodeRow(
        node_id="n1",
        store_path="memory/gone.md",
        type="memory",
        title="X",
        plane="Ideas",
        planes=["Ideas"],
        tags=[],
        aliases=[],
        disambig=None,
        occurred_start=None,
        occurred_end=None,
        merged_into=None,
        edges=[],
    )
    service, _, _, _ = _make_service(tmp_path, store=FakeSearchStore(node=node))
    preview = await service.get_node("n1")
    assert preview is not None
    assert preview.body == ""  # degrades rather than 500s


async def test_get_node_tombstone_carries_merged_into(tmp_path: Path):
    node = NodeRow(
        node_id="loser",
        store_path="person/loser.md",
        type="person",
        title="Alex",
        plane=None,
        planes=[],
        tags=[],
        aliases=[],
        disambig=None,
        occurred_start=None,
        occurred_end=None,
        merged_into="survivor",
        edges=[],
    )
    service, _, _, _ = _make_service(tmp_path, store=FakeSearchStore(node=node))
    preview = await service.get_node("loser")
    assert preview is not None and preview.merged_into == "survivor"
