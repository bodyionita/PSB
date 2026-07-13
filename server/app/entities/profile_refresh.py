"""Profile-refresh job — regenerate derived entity profiles nightly (ADR-030 §4 / ADR-034, task 6).

For every entity-like node the job computes its 1-hop neighborhood, picks the evidence tier by
degree, and — when the neighborhood changed since last time — regenerates the profile: a mechanical
**stub** with no LLM call, or an LLM-synthesized **snapshot**/**full** profile. The profile text is
embedded and stored in ``node_profiles`` (served by ``GET /nodes/{id}``).

Idempotent + LLM-frugal (ADR-034): an unchanged neighborhood (same hash) is skipped, so a stable
entity never costs a repeat model call, and the long tail of once-mentioned entities stays on the
free stub tier. Best-effort throughout (rule 7): an LLM outage degrades that entity to its stub
text and leaves its hash cleared so the next run retries the synthesis; an embed outage stores the
profile without an embedding; a single-entity failure is logged and the run continues. Depends on
protocols + the registry, so it unit-tests against fakes (08 testing policy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..providers.base import ProviderUnavailable
from ..providers.registry import ProviderRegistry
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .entity_store import EntityRef, EntityStore
from .profile_store import ProfileStore
from .profiles import (
    PROFILE_EMBED_PREFIX,
    TIER_STUB,
    ProfilePlan,
    build_profile_messages,
    clean_profile_text,
    plan_profile,
    render_stub_profile,
)

logger = logging.getLogger(__name__)

AGENT = "profile-refresh"
# A hash the job never produces (the real hash is a sha256 hex) — stored when a profile was
# degraded to its stub by an LLM outage, so the next run's real hash differs and it retries.
_RETRY_HASH = ""


@dataclass(frozen=True)
class ProfileRefreshOutcome:
    """Result of one refresh pass — feeds the ``profile-refresh`` agent_runs row + tests."""

    entities: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    tiers: dict[str, int] | None = None
    degraded: int = 0  # entities that fell back to stub text on an LLM outage

    def summary(self) -> str:
        base = (
            f"profile refresh: {self.refreshed} regenerated, {self.skipped} unchanged"
            f" of {self.entities} entity(ies)"
        )
        if self.degraded:
            base += f" ({self.degraded} degraded to stub — LLM down)"
        if self.failed:
            base += f", {self.failed} failed"
        return base

    def as_dict(self) -> dict[str, object]:
        return {
            "entities": self.entities,
            "refreshed": self.refreshed,
            "skipped": self.skipped,
            "failed": self.failed,
            "degraded": self.degraded,
            "tiers": self.tiers or {},
        }


class ProfileRefreshService:
    """Owns the nightly derived-profile regeneration (ADR-030 §4)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        profile_store: ProfileStore,
        registry: ProviderRegistry,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        self._profiles = profile_store
        self._registry = registry
        self._runs = run_store
        # Effective entity-like types (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds.
        self._vocab = vocab

    async def run_scheduled(self) -> None:
        """The scheduler/CLI entry point. Opens the run, refreshes, closes it; never raises."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for profile refresh; skipped")
            return
        try:
            outcome = await self._refresh_all()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("profile refresh failed")
            await self._safe_finish(run_id, exc)

    async def _refresh_all(self) -> ProfileRefreshOutcome:
        entity_like = (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        entities = await self._entities.list_entities(types=list(entity_like))
        refreshed = skipped = failed = degraded = 0
        tiers: dict[str, int] = {}
        for entity in entities:
            try:
                result = await self._refresh_one(entity)
            except Exception:  # noqa: BLE001 — one entity must not abort the run (rule 7)
                logger.exception("profile refresh failed for entity %s (skipped)", entity.id)
                failed += 1
                continue
            if result is None:
                skipped += 1
                continue
            tier, was_degraded = result
            refreshed += 1
            tiers[tier] = tiers.get(tier, 0) + 1
            degraded += 1 if was_degraded else 0
        return ProfileRefreshOutcome(
            entities=len(entities),
            refreshed=refreshed,
            skipped=skipped,
            failed=failed,
            tiers=tiers,
            degraded=degraded,
        )

    async def _refresh_one(self, entity: EntityRef) -> tuple[str, bool] | None:
        """Refresh one entity's profile; returns ``(tier, degraded)`` or ``None`` when unchanged."""
        neighbors = await self._entities.neighborhood(entity.id)
        plan = plan_profile(
            neighbors,
            snapshot_min=self._settings.profile_tier_snapshot_min,
            full_min=self._settings.profile_tier_full_min,
        )
        current = await self._profiles.current_hash(entity.id)
        if current is not None and current == plan.neighborhood_hash:
            return None  # neighborhood unchanged — no regen (idempotency + LLM-spend cap)

        profile_text, degraded = await self._render(entity, plan)
        stored_hash = _RETRY_HASH if degraded else plan.neighborhood_hash
        # When the LLM is down the stored text is the mechanical stub, so record the stub tier too —
        # the tier badge then matches the content (the cleared hash heals it to its real tier next
        # run). The stored observations are bounded like the LLM input (row size, matching the
        # PROFILE_MAX_OBSERVATIONS setting's intent).
        stored_tier = TIER_STUB if degraded else plan.tier
        max_obs = self._settings.profile_max_observations
        embedding = await self._embed(profile_text)
        await self._profiles.upsert_profile(
            entity.id,
            tier=stored_tier,
            profile=profile_text,
            observations=[o.as_dict() for o in plan.observations[:max_obs]],
            neighborhood_hash=stored_hash,
            embedding=embedding,
        )
        return stored_tier, degraded

    async def _render(self, entity: EntityRef, plan: ProfilePlan) -> tuple[str, bool]:
        """The profile text: mechanical stub (no LLM) or the LLM synthesis; returns
        ``(text, degraded)`` where ``degraded`` means an LLM outage forced the stub fallback."""
        stub = render_stub_profile(plan.observations)
        if not plan.needs_llm:
            return stub, False
        messages = build_profile_messages(
            title=entity.title or entity.id,
            entity_type=entity.type,
            observations=plan.observations[: self._settings.profile_max_observations],
            tier=plan.tier,
        )
        try:
            reply = await self._registry.distill(messages)
        except ProviderUnavailable:
            logger.warning("profile refresh: LLM down for %s; storing stub, will retry", entity.id)
            return stub, True
        text = clean_profile_text(reply.text)
        return (text or stub), False

    async def _embed(self, profile_text: str) -> list[float] | None:
        """Embed the profile text (ADR-030 §4 "embedded"). Best-effort: an embed outage stores the
        profile without a vector, refreshed next run."""
        if not profile_text.strip():
            return None
        try:
            result = await self._registry.embed([f"{PROFILE_EMBED_PREFIX} {profile_text}"])
        except ProviderUnavailable:
            logger.warning("profile refresh: embedder down; profile stored without embedding")
            return None
        return result.vectors[0]

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="profile refresh failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close profile-refresh agent_runs row %s", run_id)


def build_profile_refresh_service(settings: Settings, db) -> ProfileRefreshService:
    """Construct a standalone profile-refresh service for the CLI (``python -m app.cli
    profile-refresh``). Touches only the DB (no store git), so it needs no store backup."""
    from ..providers.registry import build_registry
    from ..services.agent_runs import PgAgentRunStore
    from .entity_store import PgEntityStore
    from .profile_store import PgProfileStore

    return ProfileRefreshService(
        settings=settings,
        entity_store=PgEntityStore(db),
        profile_store=PgProfileStore(db),
        registry=build_registry(settings),
        run_store=PgAgentRunStore(db),
    )
