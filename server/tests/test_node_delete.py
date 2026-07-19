"""Node-delete service tests (ADR-064 §5, M9.8 T5) — zero-degree orphan-hub delete + routing guards.

The service is exercised against fakes (entity store, index store, commit backup, run store) plus a
**real** ``NodeWriter`` over a tmp store, so the file git-rm is asserted on disk exactly as
production writes it. The routing guards (content node → capture-remove; still-referenced → Merge)
are asserted as the exceptions the admin router maps to 400 / 409.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.entities.entity_store import EntityNode, Neighbor
from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.store import NodeUpsert
from app.services.node_delete import (
    NodeDeleteIsContent,
    NodeDeleteNotFound,
    NodeDeleteNotOrphan,
    NodeDeleteService,
)
from tests.fakes import FakeAgentRunStore, FakeCommitBackup, FakeEntityStore, FakeIndexStore

CREATED = datetime(2026, 7, 12, 12, 0, 0)


def _settings(tmp_path: Path) -> Settings:
    return Settings(graph_store_path=str(tmp_path))


def _write_person(writer: NodeWriter, node_id: str, title: str) -> str:
    [w] = writer.write_nodes(
        [
            NodeDocument(
                id=node_id,
                type="person",
                title=title,
                body="",
                created_local=CREATED,
                source="text",
                aliases=(title.lower(),),
            )
        ]
    )
    return w.store_path


def _service(tmp_path, entity_store, writer, index_store, backup, runs) -> NodeDeleteService:
    return NodeDeleteService(
        settings=_settings(tmp_path),
        entity_store=entity_store,
        node_writer=writer,
        index_store=index_store,
        store_backup=backup,
        run_store=runs,
    )


async def _drive(service: NodeDeleteService, node_id: str) -> str:
    run_id = await service.delete(node_id)
    await service.drain()
    return run_id


def _neighbor(node_id: str) -> Neighbor:
    return Neighbor(
        node_id=node_id,
        type="memory",
        title="a memory",
        plane=None,
        rel="involves",
        dir="in",
        since=None,
        until=None,
        occurred_start=date(2026, 7, 12),
    )


@pytest.mark.asyncio
async def test_delete_orphan_hub_gitrms_and_prunes(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    hub_path = _write_person(writer, "hub-1", "Horia Fenwick")
    store = FakeEntityStore(
        nodes={"hub-1": EntityNode("hub-1", "person", "Horia Fenwick", hub_path, ["horia"], None)},
        neighborhoods={"hub-1": []},  # zero-degree
    )
    index = FakeIndexStore()
    # Seed the index with the node so the prune can count it (mirrors an indexed hub).
    index.nodes["hub-1"] = NodeUpsert(
        id="hub-1", store_path=hub_path, type="person", content_hash="h"
    )
    backup, runs = FakeCommitBackup(), FakeAgentRunStore()
    service = _service(tmp_path, store, writer, index, backup, runs)

    run_id = await _drive(service, "hub-1")

    # File is gone from the store, the index row is pruned, and the delete is force-committed.
    assert not (tmp_path / Path(*hub_path.split("/"))).exists()
    assert "hub-1" not in index.nodes
    assert backup.reasons and "delete orphan hub hub-1" in backup.reasons[0]
    run = runs.runs[run_id]
    assert run.status == "succeeded"
    assert run.details["files_removed"] == 1
    assert run.details["index_rows_pruned"] == 1
    assert run.details["node_id"] == "hub-1"


@pytest.mark.asyncio
async def test_delete_rejects_content_node(tmp_path: Path):
    """A content (non-entity-like) node routes to capture-remove — NodeDeleteIsContent (→400)."""
    writer = NodeWriter(str(tmp_path))
    store = FakeEntityStore(
        nodes={"mem-1": EntityNode("mem-1", "memory", "note", "memory/note--mem-1.md", [], None)},
        neighborhoods={"mem-1": []},
    )
    service = _service(
        tmp_path, store, writer, FakeIndexStore(), FakeCommitBackup(), FakeAgentRunStore()
    )
    with pytest.raises(NodeDeleteIsContent):
        await service.delete("mem-1")


@pytest.mark.asyncio
async def test_delete_rejects_referenced_hub(tmp_path: Path):
    """A hub with any live canonical neighbor isn't an orphan → NodeDeleteNotOrphan (409, Merge)."""
    writer = NodeWriter(str(tmp_path))
    hub_path = _write_person(writer, "hub-1", "Diana Vance")
    store = FakeEntityStore(
        nodes={"hub-1": EntityNode("hub-1", "person", "Diana Vance", hub_path, ["diana"], None)},
        neighborhoods={"hub-1": [_neighbor("mem-1"), _neighbor("mem-2")]},
    )
    service = _service(
        tmp_path, store, writer, FakeIndexStore(), FakeCommitBackup(), FakeAgentRunStore()
    )
    with pytest.raises(NodeDeleteNotOrphan) as excinfo:
        await service.delete("hub-1")
    assert excinfo.value.degree == 2
    # The file is untouched — a rejected delete writes nothing.
    assert (tmp_path / Path(*hub_path.split("/"))).exists()


@pytest.mark.asyncio
async def test_delete_rejects_unknown_and_tombstone(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    store = FakeEntityStore(
        nodes={
            "tomb": EntityNode("tomb", "person", "Old", "person/old--tomb.md", [], "surv-2"),
        },
    )
    service = _service(
        tmp_path, store, writer, FakeIndexStore(), FakeCommitBackup(), FakeAgentRunStore()
    )
    with pytest.raises(NodeDeleteNotFound):
        await service.delete("nope")  # unknown
    with pytest.raises(NodeDeleteNotFound):
        await service.delete("tomb")  # already a tombstone


@pytest.mark.asyncio
async def test_delete_is_self_healing_on_missing_file(tmp_path: Path):
    """The mutation tolerates an already-gone file (idempotent retry) — the run still succeeds and
    the index prune (a no-op on absent rows) still runs (rule 7)."""
    writer = NodeWriter(str(tmp_path))
    # Node validated present but its file was never written (a crash between file + index removal).
    store = FakeEntityStore(
        nodes={"hub-1": EntityNode("hub-1", "person", "Ghost", "person/ghost--hub-1.md", [], None)},
        neighborhoods={"hub-1": []},
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexStore(), FakeCommitBackup(), runs)

    run_id = await _drive(service, "hub-1")

    run = runs.runs[run_id]
    assert run.status == "succeeded"
    assert run.details["files_removed"] == 0  # file already gone
    assert run.details["index_rows_pruned"] == 0


class _RaisingBackup:
    """A StoreCommitter whose force-commit fails — to assert the run ends `failed` with context."""

    async def backup_now(self, reason: str = "manual backup"):
        raise RuntimeError("git push refused")


@pytest.mark.asyncio
async def test_delete_commit_failure_ends_run_failed(tmp_path: Path):
    """A commit hiccup after the file + index removal ends the run ``failed`` with context (rule 7),
    never crashing the service."""
    writer = NodeWriter(str(tmp_path))
    hub_path = _write_person(writer, "hub-1", "Madalina Fairfax")
    store = FakeEntityStore(
        nodes={
            "hub-1": EntityNode("hub-1", "person", "Madalina Fairfax", hub_path, ["madalina"], None)
        },
        neighborhoods={"hub-1": []},
    )
    runs = FakeAgentRunStore()
    service = _service(tmp_path, store, writer, FakeIndexStore(), _RaisingBackup(), runs)

    run_id = await _drive(service, "hub-1")

    run = runs.runs[run_id]
    assert run.status == "failed"
    assert "RuntimeError" in (run.error or "")
