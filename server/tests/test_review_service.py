"""ReviewService tests (ADR-030 §3, M3 task 4): list + resolve, with fakes + a real NodeWriter.

Covers the two resolvable kinds — entity-ambiguity (pick / new / maybe, materializing the pending
edge onto the store) and vocab-proposal (delegated to the Vocabulary service — approve resolves +
opens the consolidation run, reject discards) — plus the error paths (404/409/400 shapes) and the
resolver-side payload enrichment those depend on. The vocab-proposal *semantics* (app_settings
mutation, effective vocab, list_types) are covered in test_vocab_service; here we prove the
delegation routes correctly (ADR-035 / M3 task 7).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.entities.resolver import EntityResolver, Mention, mention_key
from app.entities.store import EntityCandidate
from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from app.indexing.store import NodeUpsert
from app.providers.registry import ProviderRegistry
from app.services.review_queue import (
    KIND_ENTITY_AMBIGUITY,
    KIND_VOCAB_PROPOSAL,
    ReviewItem,
)
from app.services.review_service import (
    BadResolution,
    ReviewNotFound,
    ReviewNotPending,
    ReviewService,
)
from app.vocab.consolidation import AGENT as AGENT_VOCAB_CONSOLIDATION
from app.vocab.consolidation import VocabConsolidation
from app.vocab.service import VocabularyService

from .fakes import (
    FakeAgentRunStore,
    FakeAliasStore,
    FakeChatProvider,
    FakeIndexer,
    FakeIndexStore,
    FakeReviewQueue,
    FakeStoreBackup,
    FakeVocabularyStore,
)

CREATED = datetime(2026, 7, 12, 12, 0, 0)
SRC_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        graph_store_path=str(tmp_path / "store"),
        planes=["Ideas"],
        scheduler_tz="UTC",
    )


def _build(tmp_path: Path):
    settings = _settings(tmp_path)
    writer = NodeWriter(settings.graph_store_path)
    review = FakeReviewQueue()
    index = FakeIndexStore()
    indexer = FakeIndexer()
    backup = FakeStoreBackup()
    runs = FakeAgentRunStore()
    # Real VocabularyService over fakes so the delegated vocab-proposal branch is exercised.
    vocab = VocabularyService(
        settings=settings,
        vocab_store=FakeVocabularyStore(),
        review_store=review,
        consolidation=VocabConsolidation(run_store=runs),
    )
    service = ReviewService(
        settings=settings,
        review_store=review,
        index_store=index,
        indexer=indexer,
        node_writer=writer,
        store_backup=backup,
        run_store=runs,
        vocab=vocab,
    )
    return service, review, index, indexer, backup, runs, writer, settings


def _seed_source_node(writer: NodeWriter, index: FakeIndexStore) -> str:
    """Write a real memory node file + register it in the index so the service can look up its
    store path (get_index_state) and append an edge to it. Returns the store path."""
    doc = NodeDocument(
        id=SRC_ID,
        type="memory",
        title="A day out",
        body="We met at the cafe.",
        created_local=CREATED,
        source="text",
    )
    [written] = writer.write_nodes([doc])
    index.nodes[SRC_ID] = NodeUpsert(
        id=SRC_ID, store_path=written.store_path, type="memory", content_hash="h"
    )
    return written.store_path


def _entity_item(pending_edges: list[dict] | None = None) -> ReviewItem:
    return ReviewItem(
        kind=KIND_ENTITY_AMBIGUITY,
        payload={
            "mention": {"name": "Alex", "type": "person", "rel": "involves"},
            "candidates": [
                {"id": "cand-a", "name": "Alex Marsh", "disambig": "colleague", "aliases": []},
                {"id": "cand-b", "name": "Alex Holt", "disambig": "cousin", "aliases": []},
            ],
            "reason": "low-confidence",
            "pending_edges": pending_edges
            if pending_edges is not None
            else [{"src": SRC_ID, "rel": "involves", "since": "2026-07-12"}],
        },
        excerpt="...met Alex at the cafe...",
        source="text",
        source_ref="cap-1",
    )


def _edges_of(root: str, store_path: str) -> set[tuple[str, str]]:
    raw = (Path(root) / Path(*store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=store_path, fallback_created=CREATED)
    return {(e.rel, e.to) for e in meta.edges}


# --- entity-ambiguity resolution ----------------------------------------------------------


async def test_pick_candidate_materializes_edge(tmp_path: Path):
    service, review, index, indexer, backup, _, writer, settings = _build(tmp_path)
    src_path = _seed_source_node(writer, index)
    rid = await review.enqueue(_entity_item())

    record = await service.resolve(rid, choice="cand-a")

    assert record.status == "resolved"
    assert record.resolution == {"choice": "cand-a"}
    assert _edges_of(settings.graph_store_path, src_path) == {("involves", "cand-a")}
    assert indexer.calls == [[src_path]]  # only the source node reindexed
    assert backup.reasons == ["review: materialize entity edge"]


async def test_pick_new_mints_entity_then_links(tmp_path: Path):
    service, review, index, indexer, backup, _, writer, settings = _build(tmp_path)
    src_path = _seed_source_node(writer, index)
    rid = await review.enqueue(_entity_item())

    record = await service.resolve(rid, choice="new")

    assert record.status == "resolved"
    entity_id = record.resolution["entity_id"]
    assert record.resolution["choice"] == "new"
    # A person hub was written with the mention name as title + alias.
    person_files = list((tmp_path / "store" / "person").glob("*.md"))
    assert len(person_files) == 1
    minted = parse_node_metadata(
        person_files[0].read_text(encoding="utf-8"),
        store_path=f"person/{person_files[0].name}",
        fallback_created=CREATED,
    )
    assert minted.id == entity_id and minted.aliases == ["Alex"]
    # The source node now links to the minted entity; the entity is indexed before the source
    # edge is materialized (so the dst_id FK is satisfied).
    assert _edges_of(settings.graph_store_path, src_path) == {("involves", entity_id)}
    assert len(indexer.calls) == 1
    assert indexer.calls[0][-1] == src_path and len(indexer.calls[0]) == 2


async def test_mint_accepts_a_freshly_approved_entity_type(tmp_path: Path):
    """Forward-live governance across services: approving a new ``entity_type`` (via the delegated
    vocab branch) makes minting a ``new`` entity of that type succeed — the mint reads the effective
    entity-like types, not the seeds (ADR-027/035, review_service._mint_entity)."""
    service, review, index, _, _, _, writer, _ = _build(tmp_path)
    _seed_source_node(writer, index)

    # A 'pet' entity of that type does not exist in the seeds → mint would reject...
    pet_item = ReviewItem(
        kind=KIND_ENTITY_AMBIGUITY,
        payload={
            "mention": {"name": "Rex", "type": "pet", "rel": "involves"},
            "candidates": [],
            "pending_edges": [{"src": SRC_ID, "rel": "involves", "since": "2026-07-12"}],
        },
    )
    reject_rid = await review.enqueue(pet_item)
    with pytest.raises(BadResolution):
        await service.resolve(reject_rid, choice="new")

    # ...until 'pet' is approved as an entity type through the shared VocabularyService.
    vocab_rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "entity_type", "value": "pet"})
    )
    await service.resolve(vocab_rid, verdict="approve")

    rid = await review.enqueue(pet_item)
    record = await service.resolve(rid, choice="new")

    assert record.status == "resolved"
    assert list((tmp_path / "store" / "pet").glob("*.md"))  # minted under the new type's folder


async def test_maybe_defers_without_materializing(tmp_path: Path):
    service, review, index, indexer, backup, _, writer, settings = _build(tmp_path)
    src_path = _seed_source_node(writer, index)
    rid = await review.enqueue(_entity_item())

    record = await service.resolve(rid, choice="maybe")

    assert record.status == "maybe"
    assert _edges_of(settings.graph_store_path, src_path) == set()  # nothing drawn
    assert indexer.calls == []
    assert backup.reasons == []


async def test_pick_skips_unindexed_source(tmp_path: Path):
    # A pending edge whose source node isn't in the index can't be materialized — skip, don't crash.
    service, review, index, indexer, backup, _, _, _ = _build(tmp_path)
    rid = await review.enqueue(
        _entity_item(pending_edges=[{"src": "ghost-id", "rel": "involves", "since": "2026-07-12"}])
    )

    record = await service.resolve(rid, choice="cand-a")

    assert record.status == "resolved"
    assert indexer.calls == []  # nothing to index
    assert backup.reasons == []


async def test_pick_non_candidate_rejected(tmp_path: Path):
    service, review, index, _, _, _, writer, _ = _build(tmp_path)
    _seed_source_node(writer, index)
    rid = await review.enqueue(_entity_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, choice="not-a-candidate")


async def test_entity_requires_choice(tmp_path: Path):
    service, review, *_ = _build(tmp_path)
    rid = await review.enqueue(_entity_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, choice=None)


# --- vocab-proposal resolution ------------------------------------------------------------


async def test_vocab_approve_delegates_and_opens_consolidation(tmp_path: Path):
    """POST /review/{id} approve is delegated to the Vocabulary service: the item resolves and a
    visible ``vocab-consolidation`` run is opened (semantics tested in test_vocab_service)."""
    service, review, _, _, _, runs, _, _ = _build(tmp_path)
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )

    record = await service.resolve(rid, verdict="approve")

    assert record.status == "resolved"
    assert record.resolution["verdict"] == "approve"
    assert record.resolution["value"] == "dream"
    # A visible run was opened under the consolidation agent (the type is now live — task 7).
    marker = next(r for r in runs.runs.values() if r.agent == AGENT_VOCAB_CONSOLIDATION)
    assert marker.status == "succeeded"
    assert record.resolution["run_id"] == marker.id


async def test_vocab_reject_discards(tmp_path: Path):
    service, review, _, _, _, runs, _, _ = _build(tmp_path)
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "edge_rel", "value": "loathes"})
    )

    record = await service.resolve(rid, verdict="reject")

    assert record.status == "discarded"
    assert record.resolution == {"verdict": "reject"}
    assert not runs.runs  # reject opens no run


async def test_vocab_requires_valid_verdict(tmp_path: Path):
    service, review, *_ = _build(tmp_path)
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )
    with pytest.raises(BadResolution):
        await service.resolve(rid, verdict="perhaps")


# --- lifecycle + listing ------------------------------------------------------------------


async def test_unknown_id_not_found(tmp_path: Path):
    service, *_ = _build(tmp_path)
    with pytest.raises(ReviewNotFound):
        await service.resolve("nope", choice="cand-a")


async def test_double_resolve_conflicts(tmp_path: Path):
    service, review, index, _, _, _, writer, _ = _build(tmp_path)
    _seed_source_node(writer, index)
    rid = await review.enqueue(_entity_item())
    await service.resolve(rid, choice="cand-a")
    with pytest.raises(ReviewNotPending):
        await service.resolve(rid, choice="cand-b")


async def test_list_defaults_to_pending(tmp_path: Path):
    service, review, index, _, _, _, writer, _ = _build(tmp_path)
    _seed_source_node(writer, index)
    keep = await review.enqueue(_entity_item())
    resolved = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )
    await service.resolve(resolved, verdict="reject")

    pending = await service.list_items()
    assert [r.id for r in pending] == [keep]

    # `all` drops the status filter; kind narrows.
    everything = await service.list_items(status="all")
    assert {r.id for r in everything} == {keep, resolved}
    only_vocab = await service.list_items(status="all", kind=KIND_VOCAB_PROPOSAL)
    assert [r.id for r in only_vocab] == [resolved]


# --- resolver-side enrichment (the write path task 4 depends on) --------------------------


async def test_resolver_review_item_carries_pending_edges(tmp_path: Path):
    """A low-confidence disambiguation files an entity-ambiguity item whose payload records the
    pending edges (src/rel/since) — the data POST /review/{id} needs to materialize the edge."""
    settings = _settings(tmp_path)
    review = FakeReviewQueue()
    candidates = {
        ("alex", "person"): [
            EntityCandidate(id="cand-a", type="person", title="Alex Marsh"),
            EntityCandidate(id="cand-b", type="person", title="Alex Holt"),
        ]
    }
    chat = FakeChatProvider("fake-chat", reply='{"choice": "none", "conf": 0.1}')
    registry = ProviderRegistry(
        {"fake-chat": chat},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    resolver = EntityResolver(
        settings=settings,
        alias_store=FakeAliasStore(candidates_by_key=candidates),
        review_queue=review,
        registry=registry,
    )
    key = mention_key("Alex", "person")
    pending = [{"src": SRC_ID, "rel": "involves", "since": "2026-07-12"}]

    result = await resolver.resolve(
        [Mention(name="Alex", type="person", rel="involves")],
        source="text",
        source_ref="cap-1",
        created_local=CREATED,
        since="2026-07-12",
        excerpt="met Alex",
        pending_edges_by_key={key: pending},
    )

    assert result.pending == 1
    assert len(review.items) == 1
    assert review.items[0].payload["pending_edges"] == pending
