"""Entity backfill scan — nightly re-check of recent memories against touched entities (ADR-030
§6, M3 task 6).

When an entity is minted or gains an alias, memories captured around the same time may mention it
without an edge (the mention was ambiguous, or the entity didn't exist yet when the memory was
organized). The backfill job closes that gap: for each entity **touched since the last run**, it
scans **recent** memory nodes whose text contains one of the entity's aliases but that carry no
edge to it, and **auto-adds** the edge (feed-flagged in the run), letting the indexer materialize
it from the rewritten file (rule 1 — the store is truth).

Guards (ADR-032 entropy guard): only aliases of length ≥ ``ENTITY_ALIAS_MIN_FUZZY_LEN`` are used —
a short alias (``Al``/``IT``) substring-matches too much to auto-link. The watermark (the last
successful run's start) means an entity is scanned once and the memory query excludes already-edged
nodes, so re-runs never duplicate an edge (rule 6). ``BACKFILL_MAX_LINKS`` bounds one run.

The lower-confidence "→ review item" branch of ADR-030 §6 is deliberately **narrowed for M3**: a
coarse substring backfill would file noisy review items on short aliases, so those are skipped
rather than reviewed. The richer review-generating drain (inbox re-organization + dedup proposals)
is M6 (04 §4 (d)/(c)); this M3 job ships the high-confidence auto-link half.

Never raises (rule 7); depends on protocols, so it unit-tests against fakes (08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..graph.node_writer import NodeEdge, NodeWriter
from ..indexing.indexer import NodeIndexer
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.store_backup import StoreCommitter
from .entity_store import EntityStore

logger = logging.getLogger(__name__)

# agent_runs.agent name for the backfill scan (visible in the activity feed, vision P8).
AGENT = "entity-backfill"
# The relation a backfilled edge asserts. Backfill has no organize-time context to pick a specific
# rel, so it links the generic "this memory involves this entity" (never a guessed specific rel).
_BACKFILL_REL = "involves"


@dataclass(frozen=True)
class BackfillOutcome:
    """Result of one backfill scan — feeds the ``entity-backfill`` agent_runs row + tests."""

    entities_scanned: int = 0
    links_added: int = 0
    nodes_changed: int = 0
    committed: bool = False
    pushed: bool = False

    def summary(self) -> str:
        return (
            f"entity backfill: {self.links_added} edge(s) auto-added across "
            f"{self.nodes_changed} memory node(s), {self.entities_scanned} entity(ies) scanned; "
            f"pushed={self.pushed}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "entities_scanned": self.entities_scanned,
            "links_added": self.links_added,
            "nodes_changed": self.nodes_changed,
            "commit": {"committed": self.committed, "pushed": self.pushed},
        }


class BackfillService:
    """Owns the nightly entity backfill scan (ADR-030 §6)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        node_writer: NodeWriter,
        indexer: NodeIndexer,
        store_backup: StoreCommitter,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        self._writer = node_writer
        self._indexer = indexer
        self._backup = store_backup
        self._runs = run_store

    async def run_scheduled(self) -> None:
        """The scheduler/CLI entry point. Opens the run, scans, closes it; never raises (rule 7)."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for backfill; skipped")
            return
        try:
            outcome = await self._scan()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("entity backfill failed")
            await self._safe_finish(run_id, exc)

    async def _scan(self) -> BackfillOutcome:
        now = datetime.now(UTC)
        watermark = await self._watermark(now)
        window_start = now - timedelta(days=self._settings.backfill_window_days)
        min_len = self._settings.entity_alias_min_fuzzy_len
        max_links = self._settings.backfill_max_links

        entities = await self._entities.entities_touched_since(
            types=list(self._settings.entity_like_types), since=watermark
        )
        changed: set[str] = set()
        links = 0
        for entity in entities:
            if links >= max_links:
                break
            # Longest aliases first (most specific), deduped case-insensitively.
            aliases = _qualifying_aliases(entity.aliases, entity.title, min_len)
            for alias in aliases:
                if links >= max_links:
                    break
                matches = await self._entities.memory_nodes_matching_alias(
                    alias, entity_id=entity.id, window_start=window_start, limit=max_links - links
                )
                for match in matches:
                    edge = NodeEdge(rel=_BACKFILL_REL, to=entity.id)
                    try:
                        await asyncio.to_thread(self._writer.add_edges, match.store_path, [edge])
                    except FileNotFoundError:
                        logger.warning(
                            "backfill: memory file %s gone; edge not added (skipped)",
                            match.store_path,
                        )
                        continue
                    changed.add(match.store_path)
                    links += 1

        committed = pushed = False
        if changed:
            await self._indexer.index_paths(sorted(changed))
            backup = await self._backup.backup_now("entity backfill: auto-linked memories")
            committed, pushed = backup.committed, backup.pushed
        return BackfillOutcome(
            entities_scanned=len(entities),
            links_added=links,
            nodes_changed=len(changed),
            committed=committed,
            pushed=pushed,
        )

    async def _watermark(self, now: datetime) -> datetime:
        """Scan only entities touched since the last successful run; first run falls back to the
        window (so a fresh store still backfills recent captures, not the whole history)."""
        default = now - timedelta(days=self._settings.backfill_window_days)
        try:
            last = await self._runs.latest(AGENT, status=SUCCEEDED)
        except Exception:  # noqa: BLE001 — a run-store read hiccup falls back to the window
            return default
        if last is None or last.started_at is None:
            return default
        return last.started_at

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="entity backfill failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close entity-backfill agent_runs row %s", run_id)


def build_backfill_service(settings: Settings, db, store_backup: StoreCommitter) -> BackfillService:
    """Construct a standalone backfill service for the CLI (``python -m app.cli
    entity-backfill``)."""
    from ..graph.node_writer import NodeWriter
    from ..indexing.indexer import Indexer
    from ..indexing.store import PgIndexStore
    from ..providers.registry import build_registry
    from ..services.agent_runs import PgAgentRunStore
    from .entity_store import PgEntityStore

    registry = build_registry(settings)
    return BackfillService(
        settings=settings,
        entity_store=PgEntityStore(db),
        node_writer=NodeWriter(settings.graph_store_path),
        indexer=Indexer(settings=settings, store=PgIndexStore(db), registry=registry),
        store_backup=store_backup,
        run_store=PgAgentRunStore(db),
    )


def _qualifying_aliases(
    aliases: list[str], title: str | None, min_len: int
) -> list[str]:
    """Aliases (+ the entity title) long enough to backfill on, most-specific (longest) first,
    deduped case-insensitively (ADR-032 entropy guard: short aliases never auto-link)."""
    seen: set[str] = set()
    out: list[str] = []
    for value in sorted([*aliases, *([title] if title else [])], key=len, reverse=True):
        norm = " ".join(value.lower().split())
        if len(norm) >= min_len and norm not in seen:
            seen.add(norm)
            out.append(value)
    return out
