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
from app.entities.entity_store import EntityNode
from app.entities.merge_core import MergeCore
from app.entities.resolver import EntityResolver, Mention, mention_key
from app.entities.store import EntityCandidate
from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from app.indexing.store import NodeUpsert
from app.providers.registry import ProviderRegistry
from app.services.review_queue import (
    KIND_DEDUP_PROPOSAL,
    KIND_ENTITY_AMBIGUITY,
    KIND_STANCE_CANDIDATE,
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
    FakeChatCaptureIngest,
    FakeChatProvider,
    FakeCommitBackup,
    FakeEntityStore,
    FakeIndexer,
    FakeIndexStore,
    FakeReviewQueue,
    FakeStoreBackup,
    FakeVocabularyStore,
    fake_routing,
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


async def test_pick_candidate_accretes_new_surface_form(tmp_path: Path):
    # ADR-040 §4: a human's disambiguation under a NEW surface form accretes it onto the hub's file.
    service, review, index, indexer, backup, _, writer, settings = _build(tmp_path)
    _seed_source_node(writer, index)
    # A real "Horia" hub on disk + indexed, so get_index_state finds its path for accretion.
    [hub] = writer.write_nodes(
        [
            NodeDocument(
                id="horia-1",
                type="person",
                title="Horia",
                body="",
                created_local=CREATED,
                source="text",
                aliases=("Horia",),
            )
        ]
    )
    index.nodes["horia-1"] = NodeUpsert(
        id="horia-1", store_path=hub.store_path, type="person", content_hash="h"
    )
    item = ReviewItem(
        kind=KIND_ENTITY_AMBIGUITY,
        payload={
            "mention": {"name": "Horia Fenwick", "type": "person", "rel": "involves"},
            "candidates": [{"id": "horia-1", "name": "Horia", "aliases": ["Horia"]}],
            "pending_edges": [{"src": SRC_ID, "rel": "involves", "since": "2026-07-12"}],
        },
        source="text",
        source_ref="cap-1",
    )
    rid = await review.enqueue(item)

    await service.resolve(rid, choice="horia-1")

    raw = (tmp_path / "store" / Path(*hub.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=hub.store_path, fallback_created=CREATED)
    assert "Horia Fenwick" in meta.aliases  # accreted onto the hub
    assert hub.store_path in [p for call in indexer.calls for p in call]  # hub reindexed


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


# --- stance-candidate resolution (M6, ADR-048 §7) -----------------------------------------


def _stance_build(tmp_path: Path, *, ingest: FakeChatCaptureIngest | None = None):
    """A ReviewService wired with a chat-capture ingest, for the stance-candidate agree path. The
    agree path touches only the review store + the ingest, so the store-facing fakes are inert."""
    settings = _settings(tmp_path)
    review = FakeReviewQueue()
    service = ReviewService(
        settings=settings,
        review_store=review,
        index_store=FakeIndexStore(),
        indexer=FakeIndexer(),
        node_writer=NodeWriter(settings.graph_store_path),
        store_backup=FakeStoreBackup(),
        run_store=FakeAgentRunStore(),
        vocab=None,
        chat_ingest=ingest,
    )
    return service, review


ANCHOR = "2026-07-10T14:30:00+00:00"


def _stance_item(anchor_at: str | None = ANCHOR) -> ReviewItem:
    payload: dict = {
        "candidate_text": "The user decided to switch to Postgres.",
        "referenced_entity_names": ["Postgres"],
        "salience": "high",
        "why_unclear": "hedged",
    }
    if anchor_at is not None:
        payload["anchor_at"] = anchor_at
    return ReviewItem(
        kind=KIND_STANCE_CANDIDATE,
        payload=payload,
        excerpt="maybe Postgres",
        source="chat",
        source_ref="session-42",
    )


async def test_stance_agree_materializes_capture(tmp_path: Path):
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    rid = await review.enqueue(_stance_item())

    record = await service.resolve(rid, verdict="agree")

    assert record.status == "resolved"
    assert record.resolution["verdict"] == "agree"
    # Exactly one capture, via the auto-endorse path: candidate text, session id, anchored time.
    assert len(ingest.captures) == 1
    cap = ingest.captures[0]
    assert cap["text"] == "The user decided to switch to Postgres."
    assert cap["session_id"] == "session-42"
    # created_at is the anchoring conversation time (ADR-048 §7), not the review-decision time.
    assert cap["created_at"] == datetime.fromisoformat(ANCHOR)
    assert record.resolution["capture_id"] == cap["capture_id"]


async def test_stance_agree_without_anchor_falls_back_to_item_created_at(tmp_path: Path):
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    rid = await review.enqueue(_stance_item(anchor_at=None))

    await service.resolve(rid, verdict="agree")

    # No anchor_at recorded ⇒ the review item's own created_at is a sane monotonic fallback.
    assert ingest.captures[0]["created_at"] == review.records[rid].created_at


async def test_stance_disagree_discards_no_capture(tmp_path: Path):
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    rid = await review.enqueue(_stance_item())

    record = await service.resolve(rid, verdict="disagree")

    assert record.status == "discarded"
    assert record.resolution == {"verdict": "disagree"}
    assert ingest.captures == []  # never a node


async def test_stance_maybe_parks_and_reopens_to_agree(tmp_path: Path):
    """`maybe` is re-openable (ADR-048 §7): a parked item accepts a later agree — the fix to the
    resolve guard (`pending` ∪ `maybe` decidable) is what makes the second decide land."""
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    rid = await review.enqueue(_stance_item())

    parked = await service.resolve(rid, verdict="maybe")
    assert parked.status == "maybe"
    assert ingest.captures == []

    # Re-open the parked maybe and agree — previously this raised (maybe was terminal).
    reopened = await service.resolve(rid, verdict="agree")
    assert reopened.status == "resolved"
    assert len(ingest.captures) == 1


async def test_stance_resolved_is_terminal(tmp_path: Path):
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    rid = await review.enqueue(_stance_item())
    await service.resolve(rid, verdict="agree")
    with pytest.raises(ReviewNotPending):
        await service.resolve(rid, verdict="disagree")


async def test_stance_bad_verdict_rejected(tmp_path: Path):
    service, review = _stance_build(tmp_path, ingest=FakeChatCaptureIngest())
    rid = await review.enqueue(_stance_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, verdict="perhaps")


async def test_stance_agree_unconfigured_is_bad_resolution(tmp_path: Path):
    # No ingest wired ⇒ agree can't materialize; disagree/maybe still work (no capture needed).
    service, review = _stance_build(tmp_path, ingest=None)
    rid = await review.enqueue(_stance_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, verdict="agree")


# --- batch resolution (M6 task 3, ADR-048 §8) ---------------------------------------------


async def test_batch_agree_resolves_every_stance_item(tmp_path: Path):
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    r1 = await review.enqueue(_stance_item())
    r2 = await review.enqueue(_stance_item())

    results = await service.resolve_batch([r1, r2], "agree")

    assert [(x.id, x.ok, x.error) for x in results] == [(r1, True, None), (r2, True, None)]
    assert len(ingest.captures) == 2  # each item routed through the auto-endorse path
    assert review.records[r1].status == "resolved" and review.records[r2].status == "resolved"


async def test_batch_is_best_effort_per_item(tmp_path: Path):
    # One good item, one already-terminal, one unknown id: every id gets a result, none aborts the
    # batch (ADR-048 §8 / rule 7).
    ingest = FakeChatCaptureIngest()
    service, review = _stance_build(tmp_path, ingest=ingest)
    good = await review.enqueue(_stance_item())
    done = await review.enqueue(_stance_item())
    await service.resolve(done, verdict="disagree")  # already terminal (discarded)
    unknown = "11111111-1111-4111-8111-111111111111"

    results = await service.resolve_batch([good, done, unknown], "disagree")

    by_id = {r.id: r for r in results}
    assert by_id[good].ok is True and by_id[good].error is None
    assert by_id[done].ok is False and by_id[done].error == "already resolved"
    assert by_id[unknown].ok is False and by_id[unknown].error == "not found"
    assert review.records[good].status == "discarded"


async def test_batch_action_invalid_for_kind_fails_that_item(tmp_path: Path):
    service, review = _stance_build(tmp_path, ingest=FakeChatCaptureIngest())
    rid = await review.enqueue(_stance_item())

    [res] = await service.resolve_batch([rid], "perhaps")

    assert res.ok is False and "verdict" in (res.error or "")  # the BadResolution reason surfaces
    assert review.records[rid].status == "pending"  # untouched → still decidable


# --- dedup-proposal resolution (M6 task 5, ADR-049) ---------------------------------------


def _dedup_build(tmp_path: Path):
    """A ReviewService wired for dedup resolution: a real NodeWriter + FakeIndexStore (for the
    `link` edge write + get_index_state), a FakeEntityStore + a real MergeCore over fakes (for the
    `merge` fold). Returns the service + the collaborators tests assert on."""
    settings = _settings(tmp_path)
    writer = NodeWriter(settings.graph_store_path)
    review = FakeReviewQueue()
    index = FakeIndexStore()
    indexer = FakeIndexer()
    backup = FakeStoreBackup()  # link uses request_commit
    commit = FakeCommitBackup()  # the core force-commits (backup_now)
    entity_store = FakeEntityStore()
    core = MergeCore(
        entity_store=entity_store, node_writer=writer, indexer=indexer, store_backup=commit
    )
    service = ReviewService(
        settings=settings,
        review_store=review,
        index_store=index,
        indexer=indexer,
        node_writer=writer,
        store_backup=backup,
        run_store=FakeAgentRunStore(),
        entity_store=entity_store,
        merge_core=core,
    )
    return service, review, index, indexer, backup, commit, entity_store, writer, settings


def _seed_content_node(writer: NodeWriter, index: FakeIndexStore, node_id: str, node_type: str):
    """Write a real content node file + register it in the index; returns the store path."""
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type=node_type,
                title=f"{node_type} {node_id}",
                body="b",
                created_local=CREATED,
                source="text",
            )
        ]
    )
    index.nodes[node_id] = NodeUpsert(
        id=node_id, store_path=w.store_path, type=node_type, content_hash="h"
    )
    return w.store_path


DEDUP_A = "11111111-1111-4111-8111-111111111111"
DEDUP_B = "22222222-2222-4222-8222-222222222222"


def _dedup_item(node_a=DEDUP_A, node_b=DEDUP_B, default_survivor=DEDUP_A) -> ReviewItem:
    return ReviewItem(
        kind=KIND_DEDUP_PROPOSAL,
        payload={
            "node_a": node_a,
            "node_b": node_b,
            "signals": {
                "cosine": 0.95,
                "shared_entity_ids": ["e1"],
                "shared_entity_titles": ["Ana"],
                "occurred_overlap": True,
            },
            "default_survivor": default_survivor,
        },
        excerpt="possible duplicate",
        source="dedup-sweep",
    )


async def test_dedup_merge_folds_loser_into_default_survivor(tmp_path: Path):
    service, review, index, indexer, backup, commit, entity_store, writer, settings = _dedup_build(
        tmp_path
    )
    a_path = _seed_content_node(writer, index, DEDUP_A, "memory")
    b_path = _seed_content_node(writer, index, DEDUP_B, "insight")
    entity_store.nodes = {
        DEDUP_A: EntityNode(DEDUP_A, "memory", "memory A", a_path, [], None),
        DEDUP_B: EntityNode(DEDUP_B, "insight", "insight B", b_path, [], None),
    }
    rid = await review.enqueue(_dedup_item(default_survivor=DEDUP_A))

    record = await service.resolve(rid, action="merge")

    assert record.status == "resolved"
    assert record.resolution == {"action": "merge", "survivor": DEDUP_A, "loser": DEDUP_B}
    # The default survivor (A) is kept; B is tombstoned onto A (cross-type merge allowed).
    b_meta = parse_node_metadata(
        (Path(settings.graph_store_path) / Path(*b_path.split("/"))).read_text("utf-8"),
        store_path=b_path,
        fallback_created=CREATED,
    )
    assert b_meta.merged_into == DEDUP_A
    assert commit.reasons and "dedup merge" in commit.reasons[0]


async def test_dedup_merge_honours_explicit_survivor(tmp_path: Path):
    service, review, index, indexer, backup, commit, entity_store, writer, settings = _dedup_build(
        tmp_path
    )
    a_path = _seed_content_node(writer, index, DEDUP_A, "memory")
    b_path = _seed_content_node(writer, index, DEDUP_B, "memory")
    entity_store.nodes = {
        DEDUP_A: EntityNode(DEDUP_A, "memory", "A", a_path, [], None),
        DEDUP_B: EntityNode(DEDUP_B, "memory", "B", b_path, [], None),
    }
    rid = await review.enqueue(_dedup_item(default_survivor=DEDUP_A))

    # Override the default survivor: fold A into B instead.
    record = await service.resolve(rid, action="merge", survivor=DEDUP_B)

    assert record.resolution == {"action": "merge", "survivor": DEDUP_B, "loser": DEDUP_A}
    a_meta = parse_node_metadata(
        (Path(settings.graph_store_path) / Path(*a_path.split("/"))).read_text("utf-8"),
        store_path=a_path,
        fallback_created=CREATED,
    )
    assert a_meta.merged_into == DEDUP_B


async def test_dedup_link_writes_canonical_similar_edge(tmp_path: Path):
    service, review, index, indexer, backup, commit, entity_store, writer, settings = _dedup_build(
        tmp_path
    )
    a_path = _seed_content_node(writer, index, DEDUP_A, "memory")
    _seed_content_node(writer, index, DEDUP_B, "insight")
    rid = await review.enqueue(_dedup_item())

    record = await service.resolve(rid, action="link")

    assert record.status == "resolved"
    assert record.resolution == {"action": "link"}
    # A canonical `similar` edge node_a → node_b lives in node_a's frontmatter (survives the derived
    # recompute — ADR-049 §2). One edge; the neighbor read unions both directions.
    assert _edges_of(settings.graph_store_path, a_path) == {("similar", DEDUP_B)}
    assert indexer.calls == [[a_path]]
    assert backup.reasons == ["review: link similar (dedup)"]
    assert commit.reasons == []  # link is additive; no force-commit fold


async def test_dedup_keep_discards_without_writing(tmp_path: Path):
    service, review, index, indexer, backup, commit, entity_store, writer, settings = _dedup_build(
        tmp_path
    )
    rid = await review.enqueue(_dedup_item())

    record = await service.resolve(rid, action="keep")

    assert record.status == "discarded"
    assert record.resolution == {"action": "keep"}
    assert indexer.calls == [] and backup.reasons == [] and commit.reasons == []


async def test_dedup_bad_action_rejected(tmp_path: Path):
    service, review, *_ = _dedup_build(tmp_path)
    rid = await review.enqueue(_dedup_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, action="frobnicate")


async def test_dedup_survivor_not_in_pair_rejected(tmp_path: Path):
    service, review, *_ = _dedup_build(tmp_path)
    rid = await review.enqueue(_dedup_item())
    with pytest.raises(BadResolution):
        await service.resolve(rid, action="merge", survivor="not-in-pair")


async def test_dedup_merge_unknown_node_rejected(tmp_path: Path):
    # The pair references a node the entity store doesn't know (e.g. removed since filing).
    service, review, *_ = _dedup_build(tmp_path)
    rid = await review.enqueue(_dedup_item())  # entity_store.nodes is empty
    with pytest.raises(BadResolution):
        await service.resolve(rid, action="merge")


async def test_dedup_batch_keep_clears_proposals(tmp_path: Path):
    # A homogeneous batch of dedup proposals resolves via `action` (default survivor, no explicit).
    service, review, *_ = _dedup_build(tmp_path)
    r1 = await review.enqueue(_dedup_item(node_a=DEDUP_A, node_b=DEDUP_B))
    r2 = await review.enqueue(
        _dedup_item(
            node_a="33333333-3333-4333-8333-333333333333",
            node_b="44444444-4444-4444-8444-444444444444",
        )
    )

    results = await service.resolve_batch([r1, r2], "keep")

    assert all(r.ok for r in results)
    assert review.records[r1].status == "discarded" and review.records[r2].status == "discarded"


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
        routing=fake_routing(registry),
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
