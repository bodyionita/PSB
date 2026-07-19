"""MergeReplayService tests (ADR-064 §1) — durable merges re-applied after a reprocess rebuild.

Exercised against fakes (entity store, decision store, indexer, commit backup) plus a **real**
``NodeWriter`` + ``MergeCore`` over a tmp store, so the re-fold's file rewrites (retarget / alias
union / tombstone) land on disk exactly as production writes them. The point under test: a merge
recorded by **surface form + type** re-folds the right freshly-rebuilt hubs even though their ids
changed, and an unresolvable/ambiguous/already-merged decision is skipped, never guessed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.entities.entity_store import EntityNode, InboundEdge
from app.entities.merge_core import MergeCore
from app.entities.merge_replay import MergeReplayService
from app.entities.merge_store import MergeDecision, surface_forms
from app.graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from app.indexing.frontmatter import parse_node_metadata

from .fakes import (
    FakeCommitBackup,
    FakeEntityStore,
    FakeIndexer,
    FakeMergeDecisionStore,
)

CREATED = datetime(2026, 7, 19, 12, 0, 0)


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


def _decision(survivor: EntityNode, loser: EntityNode) -> MergeDecision:
    return MergeDecision(
        survivor_type=survivor.type,
        survivor_forms=surface_forms(survivor.title, survivor.aliases),
        loser_type=loser.type,
        loser_forms=surface_forms(loser.title, loser.aliases),
        survivor_node_id=survivor.id,
        loser_node_id=loser.id,
    )


def _service(tmp_path: Path, entity_store: FakeEntityStore, writer: NodeWriter, decisions) -> tuple:
    indexer, backup = FakeIndexer(), FakeCommitBackup()
    core = MergeCore(
        entity_store=entity_store, node_writer=writer, indexer=indexer, store_backup=backup
    )
    service = MergeReplayService(
        decision_store=FakeMergeDecisionStore(decisions=decisions),
        entity_store=entity_store,
        merge_core=core,
        node_writer=writer,
    )
    return service, indexer, backup


def _meta(tmp_path: Path, store_path: str):
    return parse_node_metadata(
        (tmp_path / Path(*store_path.split("/"))).read_text("utf-8"),
        store_path=store_path,
        fallback_created=CREATED,
    )


async def test_replay_refolds_by_surface_form_after_id_churn(tmp_path: Path):
    """The Diana case: after a rebuild minted fresh ids, the durable merge re-folds Diana Vance →
    Diana **by name**, retargeting the loser's inbound edge; the genuinely-different Diana Wren hub
    (no decision) is left untouched."""
    writer = NodeWriter(str(tmp_path))
    diana_path = _write_entity(writer, "diana-new", "Diana", ("diana",))
    soare_path = _write_entity(writer, "vance-new", "Diana Vance", ("diana vance",))
    manda_path = _write_entity(writer, "wren-new", "Diana Wren", ("diana wren",))
    mem_path = _write_memory(writer, "mem-1", (NodeEdge(rel="involves", to="vance-new"),))

    diana = EntityNode("diana-new", "person", "Diana", diana_path, ["diana"], None)
    soare = EntityNode("vance-new", "person", "Diana Vance", soare_path, ["diana vance"], None)
    manda = EntityNode("wren-new", "person", "Diana Wren", manda_path, ["diana wren"], None)
    store = FakeEntityStore(
        nodes={"diana-new": diana, "vance-new": soare, "wren-new": manda},
        inbound={"vance-new": [InboundEdge("mem-1", mem_path, "involves")]},
    )
    # The decision was recorded pre-reprocess with the OLD ids; replay must find the hubs by form.
    service, indexer, backup = _service(tmp_path, store, writer, [_decision(diana, vance)])

    outcome = await service.replay()

    assert (outcome.decisions, outcome.applied, outcome.skipped) == (1, 1, 0)
    # Diana Vance is now a tombstone pointing at Diana.
    assert _meta(tmp_path, soare_path).merged_into == "diana-new"
    # The memory's edge was retargeted onto the survivor.
    assert [(e.rel, e.to) for e in _meta(tmp_path, mem_path).edges] == [("involves", "diana-new")]
    # Survivor unioned the loser's surface forms.
    assert "Diana Vance" in _meta(tmp_path, diana_path).aliases
    # Diana Wren — a different person, no decision — is untouched (not a tombstone).
    assert _meta(tmp_path, manda_path).merged_into is None
    assert backup.reasons and "reprocess replay merge" in backup.reasons[0]


async def test_replay_skips_when_a_side_does_not_resolve(tmp_path: Path):
    """A decision whose survivor no longer resolves (renamed/deleted) is skipped, not guessed — the
    loser stays live (never-lose)."""
    writer = NodeWriter(str(tmp_path))
    loser_path = _write_entity(writer, "vance-new", "Diana Vance", ("diana vance",))
    soare = EntityNode("vance-new", "person", "Diana Vance", loser_path, ["diana vance"], None)
    # Survivor "Diana" is absent from the rebuilt store.
    survivor = EntityNode("diana-old", "person", "Diana", "person/diana.md", ["diana"], None)
    store = FakeEntityStore(nodes={"vance-new": soare})
    service, _, _ = _service(tmp_path, store, writer, [_decision(survivor, soare)])

    outcome = await service.replay()

    assert (outcome.decisions, outcome.applied, outcome.skipped) == (1, 0, 1)
    assert _meta(tmp_path, loser_path).merged_into is None  # loser untouched


async def test_replay_skips_when_both_sides_resolve_to_same_node(tmp_path: Path):
    """Identical survivor/loser surface forms (indistinguishable after rebuild) resolve to the same
    hub — the guard skips rather than folding a node into itself."""
    writer = NodeWriter(str(tmp_path))
    path = _write_entity(writer, "diana-new", "Diana", ("diana",))
    diana = EntityNode("diana-new", "person", "Diana", path, ["diana"], None)
    dup = EntityNode("dup-old", "person", "Diana", "person/diana-2.md", ["diana"], None)
    store = FakeEntityStore(nodes={"diana-new": diana})
    service, _, _ = _service(tmp_path, store, writer, [_decision(diana, dup)])

    outcome = await service.replay()

    assert (outcome.decisions, outcome.applied, outcome.skipped) == (1, 0, 1)


async def test_replay_skips_already_merged_loser(tmp_path: Path):
    """A loser that is already a tombstone (some other pass merged it) is skipped — idempotent."""
    writer = NodeWriter(str(tmp_path))
    diana_path = _write_entity(writer, "diana-new", "Diana", ("diana",))
    soare_path = _write_entity(writer, "vance-new", "Diana Vance", ("diana vance",))
    diana = EntityNode("diana-new", "person", "Diana", diana_path, ["diana"], None)
    # Loser already merged away.
    soare = EntityNode(
        "vance-new", "person", "Diana Vance", soare_path, ["diana vance"], "diana-new"
    )
    store = FakeEntityStore(nodes={"diana-new": diana, "vance-new": soare})
    service, _, _ = _service(tmp_path, store, writer, [_decision(diana, vance)])

    outcome = await service.replay()

    assert (outcome.decisions, outcome.applied, outcome.skipped) == (1, 0, 1)
