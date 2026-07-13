"""The combined reindex job (M2, 04-pipelines §3/§5, ADR-023 §4).

One service, :class:`ReindexService`, drives the whole vault-reconciliation pass end to end:

    git pull the vault  →  reindex_all (full rescan)  →  recompute the relatedness graph
      →  one commit + push (under the single vault git lock, ADR-014)

It has two entry points that share one **single-flight** guard, so a reindex never overlaps
itself or the nightly rescan (03-api §Admin, 04 §5):

  * :meth:`start_manual` — ``POST /admin/reindex``: claims the slot, opens the ``reindex``
    ``agent_runs`` row, and runs the pass in the background so the endpoint answers ``202
    {run_id}`` immediately. Returns ``None`` when a reindex is already running (→ ``409``).
  * :meth:`run_scheduled` — the nightly scheduler job (03:40): runs the same pass inline and
    never raises (rule 7); if a manual reindex is mid-flight it logs and skips.

The relatedness graph's own block writes request a (debounced) commit; the final
``backup_now`` cancels that timer and folds everything into a single commit + push — the "one
commit+push under the vault lock" the plan calls for. The graph is recomputed **wholesale**
here and nowhere on the real-time capture path (ADR-023 §4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..db import Database
from ..graph.service import GraphOutcome
from ..indexing.indexer import IndexOutcome
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore, PgAgentRunStore
from .vault_backup import BackupResult

logger = logging.getLogger(__name__)

# agent_runs.agent name for this job (03-api §Admin, 04 §5).
AGENT = "reindex"


class Reindexer(Protocol):
    """The full-rescan surface the reindex depends on (the ``Indexer``)."""

    async def reindex_all(self) -> IndexOutcome: ...


class GraphRecomputer(Protocol):
    """The graph-recompute surface (the :class:`~app.graph.service.RelatednessGraph`)."""

    async def recompute(self) -> GraphOutcome: ...


class VaultSync(Protocol):
    """The two vault-git operations the reindex needs, both under the one lock (ADR-014)."""

    async def sync_from_remote(self) -> None: ...

    async def backup_now(self, reason: str = ...) -> BackupResult: ...


@dataclass(frozen=True)
class ReindexOutcome:
    """Result of one combined reindex pass — feeds the ``reindex`` agent_runs row + tests."""

    trigger: str
    index: IndexOutcome
    graph: GraphOutcome
    committed: bool
    pushed: bool

    @property
    def partial(self) -> bool:
        """True when the index step skipped a note on an embed failure (03-api §Admin)."""
        return self.index.partial

    def summary(self) -> str:
        """Human-readable one-liner for the activity feed (vision P8)."""
        base = (
            f"reindex ({self.trigger}): {self.index.indexed} indexed, "
            f"{self.index.skipped} skipped, {self.index.deleted} deleted, "
            f"{self.graph.links} links, {self.graph.blocks_written} block(s); "
            f"pushed={self.pushed}"
        )
        return f"{base} (partial — embed failures)" if self.partial else base

    def as_details(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "partial": self.partial,
            "index": self.index.as_dict(),
            "graph": self.graph.as_dict(),
            "commit": {"committed": self.committed, "pushed": self.pushed},
        }


class ReindexService:
    """Owns the combined reindex pass + the single-flight guard shared by both triggers."""

    def __init__(
        self,
        *,
        settings: Settings,
        indexer: Reindexer,
        graph: GraphRecomputer,
        vault_backup: VaultSync,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._indexer = indexer
        self._graph = graph
        self._backup = vault_backup
        self._runs = run_store
        # Single-flight flag. The event loop is single-threaded, so the check-and-set in the
        # claim helpers is atomic (no await between test and set) — a genuine mutual exclusion.
        self._running = False
        # Strong refs to the in-flight background task(s) so they are not GC'd mid-run.
        self._tasks: set[asyncio.Task] = set()

    @property
    def running(self) -> bool:
        return self._running

    # --- entry points ------------------------------------------------------------------------

    async def start_manual(self) -> str | None:
        """``POST /admin/reindex``: claim the slot, open the run, kick off the pass in the
        background, and return its ``run_id``. ``None`` when a reindex is already running (→409)."""
        if self._running:
            return None
        self._running = True
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:
            # Never leave the slot claimed if we could not even open the run row.
            self._running = False
            raise
        self._spawn(self._run_and_release(run_id, "manual"))
        return run_id

    async def run_scheduled(self) -> None:
        """Nightly combined reindex (the scheduler job). Never raises (rule 7); skips when a
        manual reindex is already in flight (single-flight)."""
        if self._running:
            logger.info("reindex already running; nightly job skipped (single-flight)")
            return
        self._running = True
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log + release, never crash the job
            logger.exception("could not open agent_runs row for nightly reindex; skipped")
            self._running = False
            return
        await self._run_and_release(run_id, "nightly")

    async def drain(self) -> None:
        """Await any in-flight background reindex (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- pass core ---------------------------------------------------------------------------

    async def _run_and_release(self, run_id: str, trigger: str) -> None:
        """Run the pass and *always* release the single-flight slot, whatever happens."""
        try:
            await self._execute(run_id, trigger)
        finally:
            self._running = False

    async def _execute(self, run_id: str, trigger: str) -> None:
        """The pull → rescan → recompute → commit+push pass, wrapped in its ``agent_runs`` row.

        A failure anywhere ends the run ``failed`` with context (rule 7) — the vault is truth
        (rule 1), so nothing is lost and the next nightly / a manual retry re-drives it.
        """
        try:
            # 1. Pull the vault first so the rescan sees edits made on GitHub or another device
            #    (04 §5, ADR-023 §4). Best-effort: an unreachable remote leaves the local vault.
            await self._backup.sync_from_remote()
            # 2. Full rescan: (re)index every note + reconcile deletions.
            index = await self._indexer.reindex_all()
            # 3. Recompute the whole relatedness graph + render the changed sb:related blocks.
            #    This requests a (debounced) commit for any rewritten note.
            graph = await self._graph.recompute()
            # 4. One commit + push under the single vault lock — folds in the graph's block
            #    writes and any capture debounce pending into a single reindex commit (ADR-014).
            backup = await self._backup.backup_now(f"reindex ({trigger})")

            outcome = ReindexOutcome(
                trigger=trigger,
                index=index,
                graph=graph,
                committed=backup.committed,
                pushed=backup.pushed,
            )
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=outcome.summary(),
                details=outcome.as_details(),
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("reindex (%s) failed", trigger)
            await self._safe_finish(run_id, trigger, exc)

    async def _safe_finish(self, run_id: str, trigger: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary=f"reindex ({trigger}) failed",
                details={"trigger": trigger},
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close reindex agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def build_reindex_service(
    settings: Settings, db: Database, vault_backup: VaultSync
) -> ReindexService:
    """Construct a standalone reindex service (db + git + registry) for the CLI entrypoint.

    Mirrors :func:`~app.services.backup_jobs.build_backup_jobs`: the CLI (``python -m app.cli
    reindex``) and a future external scheduler can drive the same nightly pass without the
    in-process APScheduler. The in-app wiring (``app.main``) instead reuses the already-built
    indexer/graph singletons.
    """
    # Imported here (not at module top) so the CLI's minimal context builds these lazily and the
    # request/boot path keeps the reindex service composed from its existing singletons.
    from ..graph.service import RelatednessGraph
    from ..graph.store import PgGraphStore
    from ..indexing.indexer import Indexer
    from ..indexing.store import PgIndexStore
    from ..providers.registry import build_registry

    registry = build_registry(settings)
    indexer = Indexer(settings=settings, store=PgIndexStore(db), registry=registry)
    graph = RelatednessGraph(
        settings=settings, store=PgGraphStore(db), vault_backup=vault_backup
    )
    return ReindexService(
        settings=settings,
        indexer=indexer,
        graph=graph,
        vault_backup=vault_backup,
        run_store=PgAgentRunStore(db),
    )
