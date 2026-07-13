"""Entity-merge service tests (ADR-030 §5, M3 task 6) — propose inventory + apply rewrite.

The service is exercised against fakes (entity store, indexer, commit backup, run store) plus a
**real** ``NodeWriter`` over a tmp store, so the file rewrites (retarget / alias union / tombstone)
are asserted on disk exactly as production writes them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.entities.entity_store import EntityNode, InboundEdge
from app.entities.merge import BadMerge, MergeNodeNotFound, MergeService
from app.graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from tests.fakes import FakeAgentRunStore, FakeCommitBackup, FakeEntityStore, FakeIndexer

CREATED = datetime(2026, 7, 12, 12, 0, 0)


def _settings(tmp_path: Path) -> Settings:
    return Settings(graph_store_path=str(tmp_path))


def _write_entity(writer: NodeWriter, node_id: str, title: str, aliases: tuple[str, ...]) -> str:
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type="person",
                title=title,
                body="",
                created_local=CREATED,
                source="text",
                aliases=aliases,
            )
        ]
    )
    return w.store_path


def _write_memory(writer: NodeWriter, node_id: str, edges: tuple[NodeEdge, ...]) -> str:
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type="memory",
                title=f"memory {node_id}",
                body="body",
                created_local=CREATED,
                source="text",
                edges=edges,
            )
        ]
    )
    return w.store_path


def _service(tmp_path, entity_store, writer, indexer, backup, runs) -> MergeService:
    return MergeService(
        settings=_settings(tmp_path),
        entity_store=entity_store,
        node_writer=writer,
        indexer=indexer,
        store_backup=backup,
        run_store=runs,
    )


async def _drive(service: MergeService, loser: str, survivor: str) -> str:
    run_id = await service.apply(loser, survivor)
    await service.drain()
    return run_id


@pytest.mark.asyncio
async def test_propose_returns_inbound_inventory(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    loser_path = _write_entity(writer, "loser-1", "Alex", ("alex",))
    surv_path = _write_entity(writer, "surv-2", "Alexandru", ("alexandru",))
    src_path = _write_memory(writer, "mem-1", (NodeEdge(rel="involves", to="loser-1"),))
    store = FakeEntityStore(
        nodes={
            "loser-1": EntityNode("loser-1", "person", "Alex", loser_path, ["alex"], None),
            "surv-2": EntityNode("surv-2", "person", "Alexandru", surv_path, ["alexandru"], None),
        },
        inbound={"loser-1": [InboundEdge("mem-1", src_path, "involves")]},
    )
    service = _service(
        tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    )

    proposal = await service.propose("loser-1", "surv-2")

    assert proposal.inbound_count == 1
    assert proposal.inbound[0].src_id == "mem-1"
    assert proposal.survivor.aliases == ["alexandru"]


@pytest.mark.asyncio
async def test_apply_retargets_unions_and_tombstones(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    loser_path = _write_entity(writer, "loser-1", "Alex", ("alex",))
    surv_path = _write_entity(writer, "surv-2", "Alexandru", ("alexandru",))
    src_path = _write_memory(
        writer, "mem-1", (NodeEdge(rel="involves", to="loser-1", since="2026-07-12"),)
    )
    store = FakeEntityStore(
        nodes={
            "loser-1": EntityNode("loser-1", "person", "Alex", loser_path, ["alex"], None),
            "surv-2": EntityNode("surv-2", "person", "Alexandru", surv_path, ["alexandru"], None),
        },
        inbound={"loser-1": [InboundEdge("mem-1", src_path, "involves")]},
    )
    indexer, backup, runs = FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    service = _service(tmp_path, store, writer, indexer, backup, runs)

    run_id = await _drive(service, "loser-1", "surv-2")

    # Source edge retargeted onto the survivor.
    src_meta = parse_node_metadata(
        (tmp_path / Path(*src_path.split("/"))).read_text("utf-8"),
        store_path=src_path,
        fallback_created=CREATED,
    )
    assert [(e.rel, e.to) for e in src_meta.edges] == [("involves", "surv-2")]
    # Survivor aliases unioned with the loser's name + aliases.
    surv_meta = parse_node_metadata(
        (tmp_path / Path(*surv_path.split("/"))).read_text("utf-8"),
        store_path=surv_path,
        fallback_created=CREATED,
    )
    assert surv_meta.aliases == ["alexandru", "Alexandru", "Alex", "alex"]
    # Loser is a tombstone pointing at the survivor.
    loser_meta = parse_node_metadata(
        (tmp_path / Path(*loser_path.split("/"))).read_text("utf-8"),
        store_path=loser_path,
        fallback_created=CREATED,
    )
    assert loser_meta.merged_into == "surv-2"
    # The rewritten files were reindexed and the merge force-committed.
    assert set(indexer.calls[0]) == {src_path, surv_path, loser_path}
    assert backup.reasons and "merge" in backup.reasons[0]
    assert runs.runs[run_id].status == "succeeded"
    assert runs.runs[run_id].details["edges_retargeted"] == 1


@pytest.mark.asyncio
async def test_apply_skips_missing_source_file(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    loser_path = _write_entity(writer, "loser-1", "Alex", ("alex",))
    surv_path = _write_entity(writer, "surv-2", "Alexandru", ("alexandru",))
    store = FakeEntityStore(
        nodes={
            "loser-1": EntityNode("loser-1", "person", "Alex", loser_path, ["alex"], None),
            "surv-2": EntityNode("surv-2", "person", "Alexandru", surv_path, ["alexandru"], None),
        },
        # Inbound edge whose source file was never written (vanished) — must not crash the apply.
        inbound={"loser-1": [InboundEdge("ghost", "memory/ghost--dead.md", "involves")]},
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), runs)

    run_id = await _drive(service, "loser-1", "surv-2")

    assert runs.runs[run_id].status == "succeeded"
    assert runs.runs[run_id].details["sources_skipped"] == 1
    # The loser is still tombstoned even though a source was skipped.
    assert parse_node_metadata(
        (tmp_path / Path(*loser_path.split("/"))).read_text("utf-8"),
        store_path=loser_path,
        fallback_created=CREATED,
    ).merged_into == "surv-2"


@pytest.mark.asyncio
async def test_propose_validation_errors(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    surv_path = _write_entity(writer, "surv-2", "Alexandru", ("alexandru",))
    store = FakeEntityStore(
        nodes={
            "surv-2": EntityNode("surv-2", "person", "Alexandru", surv_path, ["alexandru"], None),
            # A tombstone loser can't be merged again.
            "tomb": EntityNode("tomb", "person", "Old", "person/old.md", [], "surv-2"),
            # A content node isn't entity-like.
            "mem": EntityNode("mem", "memory", "note", "memory/n.md", [], None),
        }
    )
    service = _service(
        tmp_path, store, writer, FakeIndexer(), FakeCommitBackup(), FakeAgentRunStore()
    )

    with pytest.raises(BadMerge):
        await service.propose("surv-2", "surv-2")  # self-merge
    with pytest.raises(MergeNodeNotFound):
        await service.propose("nope", "surv-2")  # unknown loser
    with pytest.raises(BadMerge):
        await service.propose("tomb", "surv-2")  # tombstone loser
    with pytest.raises(BadMerge):
        await service.propose("mem", "surv-2")  # non-entity loser
