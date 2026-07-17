"""Identity-capsule distiller (M5 task 2, ADR-046 §5 / ADR-033 #1).

The nightly ``identity-capsule-refresh`` job (the sleep cycle, runs after profile-refresh so it
distills over fresh hubs) + an on-demand admin trigger. One ``conspect`` call blends the graph's
high-degree entity-profile hubs, recent memories, and recent insights into a compact ~300-token
capsule, stored as a rebuildable ``app_settings`` blob (rule 1) that ``build_context`` L0 and the
chat system prompt serve — the capsule is never generated inline on a read (ADR-046 §5).

Two entry points share one single-flight guard (mirrors :class:`~app.services.reindex`):

  * :meth:`trigger` — the on-demand admin trigger: claims the slot, opens the ``agent_runs`` row,
    runs the distill in the background, answers ``202 {run_id}``. ``None`` when already running.
  * :meth:`run_scheduled` — the nightly job + the CLI: runs the distill inline, never raises
    (rule 7); skips when a manual refresh is mid-flight.

Best-effort throughout (rule 7): no source material or an LLM outage **leaves the last capsule
intact** (a skipped, not failed, run) — a stale capsule beats none. Depends on protocols + the
routing service, so it unit-tests against fakes (08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ..config import Settings
from ..providers.base import ChatMessage, ProviderUnavailable
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.model_routing import ModelRoutingService
from .prompts import build_capsule_system_prompt, clean_capsule_text, render_capsule_sources
from .store import (
    CapsuleBlob,
    CapsuleSourceStore,
    CapsuleStore,
    HubProfile,
    RecentNode,
)

logger = logging.getLogger(__name__)

AGENT = "identity-capsule-refresh"


@dataclass(frozen=True)
class CapsuleOutcome:
    """Result of one refresh pass — feeds the ``identity-capsule-refresh`` agent_runs row + tests.

    ``generated`` is True only when a fresh capsule was written; ``skipped_reason`` (set when not
    generated) records why the last capsule was kept (no source material / LLM down / empty)."""

    hubs: int = 0
    memories: int = 0
    insights: int = 0
    internal: int = 0
    generated: bool = False
    chars: int = 0
    skipped_reason: str | None = None

    def summary(self) -> str:
        srcs = (
            f"{self.hubs} hub(s), {self.memories} memory(ies), {self.insights} insight(s), "
            f"{self.internal} internal"
        )
        if self.generated:
            return f"identity capsule refreshed from {srcs} ({self.chars} chars)"
        return f"identity capsule kept ({self.skipped_reason}); source was {srcs}"

    def as_dict(self) -> dict[str, object]:
        return {
            "hubs": self.hubs,
            "memories": self.memories,
            "insights": self.insights,
            "internal": self.internal,
            "generated": self.generated,
            "chars": self.chars,
            "skipped_reason": self.skipped_reason,
        }


class IdentityCapsuleService:
    """Owns the identity-capsule distill + the single-flight guard shared by both triggers."""

    def __init__(
        self,
        *,
        settings: Settings,
        capsule_store: CapsuleStore,
        sources: CapsuleSourceStore,
        routing: ModelRoutingService,
        run_store: AgentRunStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._capsule = capsule_store
        self._sources = sources
        # Distillation routes through the `conspect` group (ADR-046 §5 / ADR-025), like the other
        # nightly consolidation/profile jobs.
        self._routing = routing
        self._runs = run_store
        self._now = clock or (lambda: datetime.now(UTC))
        self._max_hubs = settings.identity_capsule_max_hubs
        self._max_memories = settings.identity_capsule_max_memories
        self._max_insights = settings.identity_capsule_max_insights
        self._max_internal = settings.identity_capsule_max_internal
        self._budget_tokens = settings.identity_capsule_budget_tokens
        self._max_chars = settings.identity_capsule_max_chars
        # Single-flight flag (the event loop is single-threaded, so check-and-set is atomic) +
        # strong refs to the in-flight background trigger so it isn't GC'd mid-run.
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def running(self) -> bool:
        return self._running

    # --- entry points ------------------------------------------------------------------------

    async def trigger(self) -> str | None:
        """On-demand admin refresh: claim the slot, open the run, distill in the background, return
        its ``run_id``. ``None`` when a refresh is already running (→ 409)."""
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

    async def run_scheduled(self) -> CapsuleOutcome | None:
        """The nightly job + CLI entry. Runs the distill inline, closes the run; never raises
        (rule 7). Skips when a manual refresh is mid-flight (single-flight). Returns the outcome on
        success (handy for CLI logging / tests) or ``None`` when the run couldn't open/failed."""
        if self._running:
            logger.info("identity-capsule refresh already running; nightly job skipped")
            return None
        self._running = True
        try:
            try:
                run_id = await self._runs.start(AGENT)
            except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
                logger.exception("could not open agent_runs row for identity-capsule refresh")
                return None
            return await self._execute(run_id, "nightly")
        finally:
            self._running = False

    async def drain(self) -> None:
        """Await any in-flight background trigger (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- pass core ---------------------------------------------------------------------------

    async def _run_and_release(self, run_id: str, trigger: str) -> None:
        """Run the pass and *always* release the single-flight slot, whatever happens."""
        try:
            await self._execute(run_id, trigger)
        finally:
            self._running = False

    async def _execute(self, run_id: str, trigger: str) -> CapsuleOutcome | None:
        """Distill + persist, wrapped in the ``agent_runs`` row. Never raises (rule 7)."""
        try:
            outcome = await self._distill()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("identity-capsule refresh (%s) failed", trigger)
            await self._safe_finish(run_id, exc)
            return None

    async def _distill(self) -> CapsuleOutcome:
        """Gather the blended source, distill on ``conspect``, and persist the blob. A thin/absent
        source or an LLM outage returns a *skipped* outcome and keeps the last capsule (rule 7)."""
        hubs = await self._sources.top_profile_hubs(self._max_hubs)
        memories = await self._sources.recent_memories(self._max_memories)
        insights = await self._sources.recent_insights(self._max_insights)
        internal = await self._sources.recent_internal(self._max_internal)
        counts = dict(
            hubs=len(hubs), memories=len(memories), insights=len(insights), internal=len(internal)
        )
        if not hubs and not memories and not insights and not internal:
            # An empty graph: nothing to distill. Keep whatever capsule exists (likely none).
            return CapsuleOutcome(**counts, skipped_reason="no source material")

        messages = [
            ChatMessage(role="system", content=build_capsule_system_prompt(self._budget_tokens)),
            ChatMessage(
                role="user",
                content=render_capsule_sources(hubs, memories, insights, internal, date.today()),
            ),
        ]
        try:
            reply = await self._routing.complete("conspect", messages)
        except ProviderUnavailable as exc:
            logger.warning("identity-capsule refresh: LLM down, keeping last capsule: %s", exc)
            return CapsuleOutcome(**counts, skipped_reason="LLM unavailable")

        text = clean_capsule_text(reply.text)[: self._max_chars].strip()
        if not text:
            return CapsuleOutcome(**counts, skipped_reason="empty distillation")

        blob = CapsuleBlob(
            text=text,
            generated_at=self._now(),
            source_refs=_source_refs(hubs, memories, insights, internal),
        )
        await self._capsule.save(blob)
        return CapsuleOutcome(**counts, generated=True, chars=len(text))

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="identity-capsule refresh failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close identity-capsule agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _source_refs(
    hubs: list[HubProfile],
    memories: list[RecentNode],
    insights: list[RecentNode],
    internal: list[RecentNode],
) -> list[dict[str, str]]:
    """The provenance refs stored on the blob (``{node_id, title, kind}`` per contributing node)."""
    refs: list[dict[str, str]] = []
    for hub in hubs:
        refs.append({"node_id": hub.node_id, "title": hub.title or "", "kind": "hub"})
    for mem in memories:
        refs.append({"node_id": mem.node_id, "title": mem.title or "", "kind": "memory"})
    for ins in insights:
        refs.append({"node_id": ins.node_id, "title": ins.title or "", "kind": "insight"})
    for node in internal:
        refs.append({"node_id": node.node_id, "title": node.title or "", "kind": "internal"})
    return refs


def build_identity_capsule_service(settings: Settings, db) -> IdentityCapsuleService:
    """Construct a standalone capsule service for the CLI (``python -m app.cli
    identity-capsule-refresh``). Touches only the DB (app_settings + reads), no store git."""
    from ..providers.registry import build_registry
    from ..services.agent_runs import PgAgentRunStore
    from ..services.model_routing import build_model_routing
    from .store import PgCapsuleSourceStore, PgIdentityCapsuleStore

    registry = build_registry(settings)
    return IdentityCapsuleService(
        settings=settings,
        capsule_store=PgIdentityCapsuleStore(db),
        sources=PgCapsuleSourceStore(db),
        routing=build_model_routing(settings, db, registry),
        run_store=PgAgentRunStore(db),
    )
