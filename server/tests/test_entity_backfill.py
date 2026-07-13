"""Entity-backfill tests (ADR-030 §6, M3 task 6) — auto-link recent memories to touched entities.

Exercised against fakes (entity store, indexer, commit backup, run store) + a real ``NodeWriter``
over a tmp store, so the auto-added edge is asserted on the memory file exactly as production."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.entities.backfill import BackfillService
from app.entities.entity_store import AliasMatchNode, EntityRef
from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.frontmatter import parse_node_metadata

from .fakes import FakeAgentRunStore, FakeCommitBackup, FakeEntityStore, FakeIndexer

CREATED = __import__("datetime").datetime(2026, 7, 12, 12, 0, 0)


def _memory(writer: NodeWriter, node_id: str, body: str) -> str:
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type="memory",
                title=f"memory {node_id}",
                body=body,
                created_local=CREATED,
                source="text",
            )
        ]
    )
    return w.store_path


def _service(tmp_path, store, writer, indexer, backup, runs) -> BackfillService:
    return BackfillService(
        settings=Settings(graph_store_path=str(tmp_path)),
        entity_store=store,
        node_writer=writer,
        indexer=indexer,
        store_backup=backup,
        run_store=runs,
    )


def _entity(aliases, title="Alexandru", node_id="e1"):
    return EntityRef(
        id=node_id, type="person", title=title, aliases=aliases, store_path="person/a.md"
    )


@pytest.mark.asyncio
async def test_qualifying_alias_auto_links_recent_memory(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    mem_path = _memory(writer, "mem-1", "Had coffee with Alexandru today.")
    store = FakeEntityStore(
        entities=[_entity(["alexandru"])],
        alias_matches={"alexandru": [AliasMatchNode("mem-1", mem_path, "…Alexandru…")]},
    )
    indexer, backup, runs = FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    service = _service(tmp_path, store, writer, indexer, backup, runs)

    await service.run_scheduled()

    meta = parse_node_metadata(
        (tmp_path / Path(*mem_path.split("/"))).read_text("utf-8"),
        store_path=mem_path,
        fallback_created=CREATED,
    )
    assert [(e.rel, e.to) for e in meta.edges] == [("involves", "e1")]
    assert indexer.calls == [[mem_path]]
    assert backup.reasons  # force-committed
    run = list(runs.runs.values())[0]
    assert run.status == "succeeded"
    assert run.details["links_added"] == 1


@pytest.mark.asyncio
async def test_short_alias_is_not_used(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    mem_path = _memory(writer, "mem-1", "Al was here.")
    # "Al" (2 chars) is below ENTITY_ALIAS_MIN_FUZZY_LEN(4) → never scanned, so even a seeded match
    # is unreachable. (title "Al" is also short.)
    store = FakeEntityStore(
        entities=[_entity(["al"], title="Al")],
        alias_matches={"al": [AliasMatchNode("mem-1", mem_path, "…Al…")]},
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), runs)

    await service.run_scheduled()

    meta = parse_node_metadata(
        (tmp_path / Path(*mem_path.split("/"))).read_text("utf-8"),
        store_path=mem_path,
        fallback_created=CREATED,
    )
    assert meta.edges == []
    assert list(runs.runs.values())[0].details["links_added"] == 0


@pytest.mark.asyncio
async def test_same_memory_matched_by_two_aliases_counts_once(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    mem_path = _memory(writer, "mem-1", "Alexandru Popescu came over.")
    match = AliasMatchNode("mem-1", mem_path, "…Alexandru Popescu…")
    # Two distinct qualifying aliases of the SAME entity both match the same memory. The first adds
    # the edge; the second must not re-count it (add_edges returns False when nothing changed).
    store = FakeEntityStore(
        entities=[_entity(["alexandru", "popescu"])],
        alias_matches={"alexandru": [match], "popescu": [match]},
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), runs)

    await service.run_scheduled()

    meta = parse_node_metadata(
        (tmp_path / Path(*mem_path.split("/"))).read_text("utf-8"),
        store_path=mem_path,
        fallback_created=CREATED,
    )
    assert [(e.rel, e.to) for e in meta.edges] == [("involves", "e1")]  # one edge
    run = list(runs.runs.values())[0]
    assert run.details["links_added"] == 1
    assert run.details["nodes_changed"] == 1


@pytest.mark.asyncio
async def test_scan_uses_last_successful_run_as_watermark(tmp_path: Path):
    from datetime import UTC, datetime

    writer = NodeWriter(str(tmp_path))
    store = FakeEntityStore(entities=[_entity(["alexandru"])])
    runs = FakeAgentRunStore()
    # A prior successful backfill run fixes the watermark; entities are only re-scanned from there.
    watermark = datetime(2026, 7, 1, 3, 20, tzinfo=UTC)
    from app.services.agent_runs import SUCCEEDED, AgentRun

    runs.preloaded["entity-backfill"] = AgentRun(
        id="prev", agent="entity-backfill", status=SUCCEEDED, started_at=watermark
    )
    service = _service(tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), runs)

    await service.run_scheduled()

    assert store.touched_since_arg == watermark


@pytest.mark.asyncio
async def test_missing_memory_file_is_skipped(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    store = FakeEntityStore(
        entities=[_entity(["alexandru"])],
        alias_matches={
            "alexandru": [AliasMatchNode("ghost", "memory/ghost--dead.md", "…Alexandru…")]
        },
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), runs)

    await service.run_scheduled()

    run = list(runs.runs.values())[0]
    assert run.status == "succeeded"
    assert run.details["links_added"] == 0
    assert run.details["nodes_changed"] == 0
