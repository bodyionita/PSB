"""MergeCore tests (ADR-049 §1) — the shared retarget → tombstone → reindex → commit fold.

Exercised against fakes (entity store, indexer, commit backup) plus a **real** ``NodeWriter`` over a
tmp store, so the file rewrites (retarget / tombstone) are asserted on disk exactly as production
writes them. The entity-merge composition (core + alias union) is covered end-to-end in
test_entity_merge; here we prove the extracted core in isolation, including the content-merge shape
(fold alone, no alias union, cross-type allowed).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.entities.entity_store import EntityNode, InboundEdge
from app.entities.merge_core import MergeCore, MergeTarget
from app.graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from tests.fakes import FakeCommitBackup, FakeEntityStore, FakeIndexer

CREATED = datetime(2026, 7, 12, 12, 0, 0)


def _write_content(writer: NodeWriter, node_id: str, node_type: str, edges=()) -> str:
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type=node_type,
                title=f"{node_type} {node_id}",
                body="body",
                created_local=CREATED,
                source="text",
                edges=tuple(edges),
            )
        ]
    )
    return w.store_path


def _target(node_id: str, store_path: str, node_type: str = "memory") -> MergeTarget:
    return MergeTarget(id=node_id, type=node_type, title=None, store_path=store_path)


def _core(entity_store, writer, indexer, backup) -> MergeCore:
    return MergeCore(
        entity_store=entity_store, node_writer=writer, indexer=indexer, store_backup=backup
    )


@pytest.mark.asyncio
async def test_fold_retargets_inbound_and_tombstones(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    loser = _write_content(writer, "loser-1", "memory")
    survivor = _write_content(writer, "surv-2", "insight")
    # A source node pointing at the loser (a content→content edge) gets retargeted onto survivor.
    src = _write_content(writer, "mem-9", "memory", [NodeEdge(rel="follows", to="loser-1")])
    store = FakeEntityStore(inbound={"loser-1": [InboundEdge("mem-9", src, "follows")]})
    indexer, backup = FakeIndexer(), FakeCommitBackup()

    result = await _core(store, writer, indexer, backup).fold(
        loser=_target("loser-1", loser, "memory"),
        survivor=_target("surv-2", survivor, "insight"),
        reason="dedup merge loser-1 → surv-2",
    )

    # Cross-type content merge is allowed (memory → insight) — the core imposes no type check.
    src_meta = parse_node_metadata(
        (tmp_path / Path(*src.split("/"))).read_text("utf-8"),
        store_path=src,
        fallback_created=CREATED,
    )
    assert [(e.rel, e.to) for e in src_meta.edges] == [("follows", "surv-2")]
    loser_meta = parse_node_metadata(
        (tmp_path / Path(*loser.split("/"))).read_text("utf-8"),
        store_path=loser,
        fallback_created=CREATED,
    )
    assert loser_meta.merged_into == "surv-2"
    # The rewritten source + the tombstoned loser were reindexed and the merge force-committed.
    assert set(indexer.calls[0]) == {src, loser}
    assert backup.reasons == ["dedup merge loser-1 → surv-2"]
    assert result.edges_retargeted == 1
    assert result.sources_skipped == 0
    assert result.committed and result.pushed


@pytest.mark.asyncio
async def test_fold_reindexes_survivor_extra_paths(tmp_path: Path):
    # The entity-merge composition hands the alias-rewritten survivor file via survivor_extra_paths;
    # the core reindexes it in the same pass (dedup-into no inbound-edge case).
    writer = NodeWriter(str(tmp_path))
    loser = _write_content(writer, "loser-1", "memory")
    survivor = _write_content(writer, "surv-2", "memory")
    store = FakeEntityStore(inbound={})  # no inbound edges to the loser
    indexer, backup = FakeIndexer(), FakeCommitBackup()

    result = await _core(store, writer, indexer, backup).fold(
        loser=_target("loser-1", loser),
        survivor=_target("surv-2", survivor),
        reason="merge",
        survivor_extra_paths=[survivor],
    )

    # Survivor (extra path) + tombstoned loser reindexed; no edge retargeted.
    assert set(indexer.calls[0]) == {survivor, loser}
    assert result.edges_retargeted == 0
    assert result.files_changed == 2


@pytest.mark.asyncio
async def test_fold_skips_missing_source_never_crashes(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    loser = _write_content(writer, "loser-1", "memory")
    survivor = _write_content(writer, "surv-2", "memory")
    # Inbound edge whose source file was never written (vanished) — must not crash the fold.
    store = FakeEntityStore(
        inbound={"loser-1": [InboundEdge("ghost", "memory/ghost--dead.md", "follows")]}
    )
    indexer, backup = FakeIndexer(), FakeCommitBackup()

    result = await _core(store, writer, indexer, backup).fold(
        loser=_target("loser-1", loser), survivor=_target("surv-2", survivor), reason="merge"
    )

    assert result.sources_skipped == 1
    assert result.edges_retargeted == 0
    # The loser is still tombstoned even though a source was skipped.
    assert (
        parse_node_metadata(
            (tmp_path / Path(*loser.split("/"))).read_text("utf-8"),
            store_path=loser,
            fallback_created=CREATED,
        ).merged_into
        == "surv-2"
    )


@pytest.mark.asyncio
async def test_entity_node_projects_onto_target(tmp_path: Path):
    # Sanity: an EntityNode's fields map cleanly onto a MergeTarget (the projection callers use).
    node = EntityNode("id-1", "person", "Alex", "person/alex--1.md", ["alex"], None)
    target = MergeTarget(id=node.id, type=node.type, title=node.title, store_path=node.store_path)
    assert (target.id, target.type, target.store_path) == ("id-1", "person", "person/alex--1.md")
