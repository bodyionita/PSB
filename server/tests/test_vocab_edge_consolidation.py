"""Edge retro-consolidation (ADR-036 / M3 task 7b): pure helpers (no mocks) + service (fakes).

The pure prompt/parse/sanitise helpers are unit-tested directly; the service is exercised with a
fake edge store + fake chat + a tmp graph store (no DB, no LLM — 08 testing policy).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from app.providers.base import ProviderUnavailable
from app.providers.registry import ProviderRegistry
from app.vocab.edge_consolidation import (
    BadConsolidation,
    EdgeConsolidationService,
    EdgeRetype,
    clean_retypes,
    parse_retype_plan,
    render_edge_inventory,
    retypes_from_indices,
)
from app.vocab.edge_store import EdgeCandidate

from .fakes import (
    FakeAgentRunStore,
    FakeChatProvider,
    FakeCommitBackup,
    FakeEdgeConsolidationStore,
    FakeIndexer,
)

CREATED = datetime(2026, 7, 12, 12, 0, 0)
# An edge vocabulary with a newly-approved "mentors" rel present (forward-live seed for the tests).
_EDGE_RELS = ["involves", "about", "part_of", "led_to", "follows", "at", "mentors"]


def _candidate(src_id: str, rel: str, dst_id: str, *, excerpt: str | None = None) -> EdgeCandidate:
    return EdgeCandidate(
        src_id=src_id,
        src_title=f"node {src_id}",
        src_excerpt=excerpt,
        rel=rel,
        dst_id=dst_id,
        dst_title=f"node {dst_id}",
    )


# --- pure: render_edge_inventory ----------------------------------------------------------------


def test_render_edge_inventory_numbers_edges_and_one_lines_excerpt():
    block = render_edge_inventory(
        [_candidate("s1", "involves", "d1", excerpt="line one\n  line two")]
    )
    assert '[0] "node s1" --involves--> "node d1"' in block
    assert "source: line one line two" in block  # excerpt collapsed to one line


def test_render_edge_inventory_untitled_fallback():
    c = EdgeCandidate(
        src_id="s", src_title=None, src_excerpt=None, rel="about", dst_id="d", dst_title=None
    )
    block = render_edge_inventory([c])
    assert block == '[0] "(untitled)" --about--> "(untitled)"'  # no source line when no excerpt


# --- pure: parse_retype_plan --------------------------------------------------------------------


def test_parse_retype_plan_tolerates_code_fence():
    assert parse_retype_plan('```json\n{"retype": [0, 2]}\n```') == [0, 2]


def test_parse_retype_plan_rejects_non_conforming_and_bools():
    assert parse_retype_plan("not json") == []
    assert parse_retype_plan('{"retype": "nope"}') == []
    # bool is an int subclass — must not be read as an index.
    assert parse_retype_plan('{"retype": [true, 1]}') == [1]


# --- pure: retypes_from_indices + clean_retypes -------------------------------------------------


def test_retypes_from_indices_resolves_and_drops_out_of_range():
    candidates = [_candidate("s1", "involves", "d1"), _candidate("s2", "about", "d2")]
    out = retypes_from_indices([0, 9], candidates, to_rel="mentors")
    assert out == [EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors")]


def test_clean_retypes_drops_noops_and_dedupes():
    pairs = [
        ("s1", "d1", "involves"),
        ("s1", "d1", "involves"),  # exact duplicate
        ("s2", "d2", "mentors"),  # no-op: already the target rel
        ("  ", "d3", "about"),  # empty src drops
    ]
    out = clean_retypes(pairs, to_rel="mentors")
    assert out == [EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors")]


# --- service scaffolding ------------------------------------------------------------------------


def _registry(chat: FakeChatProvider) -> ProviderRegistry:
    return ProviderRegistry(
        {"fake-chat": chat},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )


def _service(
    tmp_path: Path,
    *,
    store: FakeEdgeConsolidationStore,
    chat: FakeChatProvider | None = None,
    indexer: FakeIndexer | None = None,
    backup: FakeCommitBackup | None = None,
    runs: FakeAgentRunStore | None = None,
) -> EdgeConsolidationService:
    settings = Settings(graph_store_path=str(tmp_path / "store"), edge_rels=_EDGE_RELS)
    return EdgeConsolidationService(
        settings=settings,
        store=store,
        node_writer=NodeWriter(settings.graph_store_path),
        registry=_registry(chat or FakeChatProvider("fake-chat", reply="{}")),
        indexer=indexer or FakeIndexer(),
        store_backup=backup or FakeCommitBackup(),
        run_store=runs or FakeAgentRunStore(),
    )


def _doc(node_id: str, edges: tuple[NodeEdge, ...]) -> NodeDocument:
    return NodeDocument(
        id=node_id,
        type="memory",
        title=f"node {node_id}",
        body="body",
        created_local=CREATED,
        source="text",
        edges=edges,
    )


# --- service: propose ---------------------------------------------------------------------------


async def test_propose_returns_sanitised_retypings(tmp_path: Path):
    candidates = [_candidate("s1", "involves", "d1"), _candidate("s2", "about", "d2")]
    store = FakeEdgeConsolidationStore(candidates=candidates)
    chat = FakeChatProvider("fake-chat", reply=json.dumps({"retype": [0]}))
    service = _service(tmp_path, store=store, chat=chat)

    proposal = await service.propose("mentors")

    assert proposal.plan_id and proposal.rel == "mentors"
    assert proposal.retypings == [
        EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors")
    ]
    # The target rel is excluded from the inventory and the cap is the config value.
    assert store.inventory_args == {
        "exclude_rel": "mentors",
        "limit": Settings().vocab_consolidate_max_edges,
    }


async def test_propose_skips_model_when_no_candidates(tmp_path: Path):
    store = FakeEdgeConsolidationStore(candidates=[])
    chat = FakeChatProvider("fake-chat", reply="{}")
    service = _service(tmp_path, store=store, chat=chat)

    proposal = await service.propose("mentors")

    assert proposal.retypings == []
    assert chat.calls == 0  # no model call on an edge-less graph


async def test_propose_unknown_rel_raises(tmp_path: Path):
    service = _service(tmp_path, store=FakeEdgeConsolidationStore())
    with pytest.raises(BadConsolidation):
        await service.propose("not-a-rel")


async def test_propose_propagates_provider_unavailable(tmp_path: Path):
    store = FakeEdgeConsolidationStore(candidates=[_candidate("s1", "involves", "d1")])
    chat = FakeChatProvider("fake-chat", available=False)
    service = _service(tmp_path, store=store, chat=chat)
    with pytest.raises(ProviderUnavailable):
        await service.propose("mentors")


# --- service: apply -----------------------------------------------------------------------------


async def test_apply_retypes_edges_reindexes_and_commits(tmp_path: Path):
    store_root = tmp_path / "store"
    writer = NodeWriter(str(store_root))
    [n1] = writer.write_nodes([_doc("s1", (NodeEdge(rel="involves", to="d1"),))])
    [n2] = writer.write_nodes([_doc("s2", (NodeEdge(rel="involves", to="d2"),))])
    edge_store = FakeEdgeConsolidationStore(paths={"s1": n1.store_path, "s2": n2.store_path})
    indexer, backup, runs = FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    service = _service(tmp_path, store=edge_store, indexer=indexer, backup=backup, runs=runs)

    plan = [
        EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors"),
        EdgeRetype(src_id="s2", to="d2", from_rel="involves", to_rel="mentors"),
    ]
    run_id = await service.apply("mentors", plan)
    await service.drain()

    for n in (n1, n2):
        meta = parse_node_metadata(
            (store_root / Path(*n.store_path.split("/"))).read_text(encoding="utf-8"),
            store_path=n.store_path,
            fallback_created=CREATED,
        )
        assert [e.rel for e in meta.edges] == ["mentors"]
    assert sorted(indexer.calls[0]) == sorted([n1.store_path, n2.store_path])
    assert backup.reasons == ["vocab consolidate: edges → mentors"]
    run = runs.runs[run_id]
    assert run.status == "succeeded"
    assert run.details["edges_retyped"] == 2 and run.details["files_changed"] == 2


async def test_apply_retypes_two_edges_on_the_same_source_file(tmp_path: Path):
    # Two edges on one file are re-typed sequentially (the second read sees the first's write);
    # the file is reindexed once (path de-duped in `changed`).
    store_root = tmp_path / "store"
    writer = NodeWriter(str(store_root))
    [n1] = writer.write_nodes(
        [_doc("s1", (NodeEdge(rel="involves", to="d1"), NodeEdge(rel="involves", to="d2")))]
    )
    edge_store = FakeEdgeConsolidationStore(paths={"s1": n1.store_path})
    indexer, runs = FakeIndexer(), FakeAgentRunStore()
    service = _service(tmp_path, store=edge_store, indexer=indexer, runs=runs)

    plan = [
        EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors"),
        EdgeRetype(src_id="s1", to="d2", from_rel="involves", to_rel="mentors"),
    ]
    run_id = await service.apply("mentors", plan)
    await service.drain()

    meta = parse_node_metadata(
        (store_root / Path(*n1.store_path.split("/"))).read_text(encoding="utf-8"),
        store_path=n1.store_path,
        fallback_created=CREATED,
    )
    assert {(e.rel, e.to) for e in meta.edges} == {("mentors", "d1"), ("mentors", "d2")}
    assert indexer.calls[0] == [n1.store_path]  # one file, reindexed once
    assert runs.runs[run_id].details["edges_retyped"] == 2


async def test_apply_skips_unresolvable_source_and_still_succeeds(tmp_path: Path):
    store_root = tmp_path / "store"
    writer = NodeWriter(str(store_root))
    [n1] = writer.write_nodes([_doc("s1", (NodeEdge(rel="involves", to="d1"),))])
    # s2 has no store path (un-indexed / tombstoned) → skipped, run still succeeds.
    edge_store = FakeEdgeConsolidationStore(paths={"s1": n1.store_path})
    runs, backup = FakeAgentRunStore(), FakeCommitBackup()
    service = _service(tmp_path, store=edge_store, runs=runs, backup=backup)

    plan = [
        EdgeRetype(src_id="s1", to="d1", from_rel="involves", to_rel="mentors"),
        EdgeRetype(src_id="s2", to="d2", from_rel="involves", to_rel="mentors"),
    ]
    run_id = await service.apply("mentors", plan)
    await service.drain()

    run = runs.runs[run_id]
    assert run.status == "succeeded"
    assert run.details["edges_retyped"] == 1 and run.details["edges_skipped"] == 1


async def test_apply_no_op_plan_makes_no_commit(tmp_path: Path):
    edge_store = FakeEdgeConsolidationStore(paths={})
    indexer, backup, runs = FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    service = _service(tmp_path, store=edge_store, indexer=indexer, backup=backup, runs=runs)

    # Every item is a no-op (already the target rel) → sanitises to empty → no writes.
    plan = [EdgeRetype(src_id="s1", to="d1", from_rel="mentors", to_rel="mentors")]
    run_id = await service.apply("mentors", plan)
    await service.drain()

    assert indexer.calls == [] and backup.reasons == []
    assert runs.runs[run_id].status == "succeeded"


async def test_apply_unknown_rel_raises(tmp_path: Path):
    service = _service(tmp_path, store=FakeEdgeConsolidationStore())
    with pytest.raises(BadConsolidation):
        await service.apply("not-a-rel", [])
