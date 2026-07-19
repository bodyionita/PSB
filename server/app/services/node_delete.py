"""Node-delete path for zero-degree orphan hubs (ADR-064 §5, M9.8 T5).

Graph-health's orphan GC (ADR-064 §5) offers **Delete** on a genuinely zero-degree node, routed by
node kind so nothing resurrects:

* an orphan **content** node (from a capture) → **tombstone its capture** (via
  ``CaptureRemovalService``) — reprocess would otherwise replay the raw and recreate it;
* an orphan **hub** (an entity-like node, not a capture) → **node-level git-rm + index prune** — the
  net-new path this module owns. Reprocess won't recreate an *unreferenced* hub, so deleting the
  file is safe. Offered **only** for genuinely zero-degree hubs — a still-referenced hub isn't an
  orphan and is routed to Merge instead (ADR-064 §5).

This service owns only the **hub** leg: it validates the node is a zero-degree entity-like hub, then
git-rms its file (:meth:`NodeWriter.remove_nodes`) + prunes its index rows
(:class:`~app.services.capture_removal.NodeDeleteStore` — ``chunks``/``edges``/``node_media``
cascade off ``nodes``, ADR-026/060) + force-commits (ADR-014 is the safety net — a wrong delete is a
``git revert``). A **content** node is rejected with :class:`NodeDeleteIsContent` so the caller
routes it to capture-remove; a **still-referenced** node with :class:`NodeDeleteNotOrphan` so the
caller routes it to Merge. Never auto-deletes (rule 2 / data-survival) — the human picks Delete.

The store is truth (rule 1): the service removes the *file* and prunes the derived index rows; it
never guesses. Validation runs synchronously so a bad request still gets a 400/404/409 (not a failed
run); the mutation runs in the background under an ``agent_runs`` row (P8 visibility) so the
endpoint answers ``202 {run_id}``, mirroring the entity-merge apply. It depends on protocols, so it
unit-tests against fakes (no live DB/LLM — 08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..entities.entity_store import EntityNode, EntityStore
from ..graph.node_writer import NodeWriter
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.capture_removal import NodeDeleteStore
from ..services.store_backup import StoreCommitter
from ..vocab.service import VocabularyProvider, effective_vocabulary

logger = logging.getLogger(__name__)

# agent_runs.agent name for the delete apply (visible in the activity feed, vision P8).
AGENT = "node-delete"


class NodeDeleteError(Exception):
    """Base for node-delete problems surfaced to the API layer."""


class NodeDeleteNotFound(NodeDeleteError):
    """The node id is unknown or already a tombstone/deleted (404)."""


class NodeDeleteIsContent(NodeDeleteError):
    """The node is a **content** node, not an entity hub (400) — route it to capture-remove, which
    tombstones the owning capture so a reprocess can't replay the raw and resurrect it (ADR-064 §5).
    This path git-rms a bare file, which a reprocess *would* recreate for a content node."""


class NodeDeleteNotOrphan(NodeDeleteError):
    """The node still has ``degree`` canonical edge(s) (409) — it isn't an orphan. A
    still-referenced hub is routed to Merge (ADR-064 §5), never deleted (deleting it would dangle
    every edge into it)."""

    def __init__(self, degree: int) -> None:
        self.degree = degree
        super().__init__(f"node still has {degree} canonical edge(s); merge it instead")


class NodeDeleteService:
    """Owns the zero-degree orphan-hub delete (git-rm + index prune + commit, ADR-064 §5)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        node_writer: NodeWriter,
        index_store: NodeDeleteStore,
        store_backup: StoreCommitter,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        self._writer = node_writer
        self._index = index_store
        self._backup = store_backup
        self._runs = run_store
        # Effective entity-like types (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds. Only
        # a hub of one of these types is deletable here — a content node routes to capture-remove.
        self._vocab = vocab
        self._tasks: set[asyncio.Task] = set()

    async def delete(self, node_id: str) -> str:
        """Re-validate, open the run, and git-rm + prune + commit in the background. Returns run_id.

        Validation runs synchronously so a bad request gets a 400/404/409 (not a failed run); the
        store mutation runs in the background so the endpoint answers ``202 {run_id}``. Raises
        :class:`NodeDeleteNotFound` (unknown/tombstone), :class:`NodeDeleteIsContent` (route to
        capture-remove), or :class:`NodeDeleteNotOrphan` (route to Merge).

        The background task acts on the validation-time snapshot (same posture as
        :meth:`MergeService.apply`); a zero-degree hub has nothing referencing it, so the
        validate→delete window carries no realistic race — a new edge would have to target a node
        the graph currently points at from nowhere."""
        node = await self._validated_orphan_hub(node_id)
        run_id = await self._runs.start(AGENT)
        self._spawn(self._run_delete(run_id, node))
        return run_id

    async def _validated_orphan_hub(self, node_id: str) -> EntityNode:
        """Fetch + validate the node is a **zero-degree entity-like hub** — the only thing this path
        deletes. A tombstone/unknown → 404; a content node → route to capture-remove (400); any
        canonical edge either direction → route to Merge (409)."""
        node = await self._entities.get_node(node_id)
        if node is None or node.merged_into is not None:
            raise NodeDeleteNotFound(node_id)
        entity_types = set(
            (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        )
        if node.type not in entity_types:
            raise NodeDeleteIsContent(node_id)
        # Zero-degree = no live canonical neighbor either direction (the neighborhood read excludes
        # tombstoned endpoints, so a reference from a merged-away node doesn't keep a hub alive —
        # matching the entity layer's liveness). A non-empty neighborhood ⇒ referenced ⇒ merge.
        neighbors = await self._entities.neighborhood(node_id)
        if neighbors:
            raise NodeDeleteNotOrphan(len(neighbors))
        return node

    async def _run_delete(self, run_id: str, node: EntityNode) -> None:
        """Git-rm the hub file + prune its index rows + force-commit, under the open run (§5).

        Never raises (rule 7): the file unlink tolerates an already-gone file and the index prune is
        keyed to the store path (no-op on absent rows), so a retry is safe; any unexpected failure
        ends the run ``failed`` with context. Files are truth + git-revertible, so a partial delete
        is safe to re-drive."""
        try:
            removed = await asyncio.to_thread(self._writer.remove_nodes, [node.store_path])
            pruned = await self._index.delete_nodes([node.store_path])
            backup = await self._backup.backup_now(f"delete orphan hub {node.id}")
            summary = (
                f"deleted orphan hub {node.title or node.id} "
                f"({len(removed)} file(s) removed, {pruned} index row(s) pruned)"
            )
            logger.info("%s", summary)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "node_id": node.id,
                    "type": node.type,
                    "store_path": node.store_path,
                    "files_removed": len(removed),
                    "index_rows_pruned": pruned,
                    "commit": {"committed": backup.committed, "pushed": backup.pushed},
                },
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("node delete apply failed")
            await self._safe_finish(run_id, exc)

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="node delete failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close node-delete agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Await any in-flight background delete (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
