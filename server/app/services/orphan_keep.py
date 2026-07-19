"""Orphan keep-list service (ADR-064 §5, M9.8 T5.5) — keep / list / un-keep intentionally-kept hubs.

Graph-health's orphan GC (ADR-064 §5) offers **Keep** on an orphan hub: whitelist it so the nightly
orphan check stops flagging it (e.g. the intentionally-kept Father/Mother hubs). Unlike Delete (T5,
a background git-rm run) a keep is a **config-like decision** — recorded **synchronously**, no
``agent_runs`` job — mirroring a merge decision or a vocab approval.

This service owns the three keep operations behind the admin endpoints:

* :meth:`keep` — resolve the node's **current** surface forms + type and upsert an
  :class:`~app.entities.keep_store.KeepDecision` keyed on **surface form + type, not node id**
  (survives ``reprocess-all`` as a read-time filter). **Hubs-only**: an unknown/tombstone node →
  :class:`OrphanKeepNotFound` (404); a **content** node → :class:`OrphanKeepIsContent` (400). No
  zero-degree check — keeping a hub that isn't (yet) an orphan is harmless (it simply won't be
  flagged if it later becomes one), and Keep carries no 409 (03-api §Admin). Idempotent (upsert).
* :meth:`list_keeps` — the kept-hub list backing the web "Kept (N)" strip.
* :meth:`unkeep` — remove a keep by its stable ``keep_key`` (**not** node id — a reprocess changes
  the id, the key persists); an unknown key → :class:`OrphanKeepKeyNotFound` (404).

It depends on the :class:`~app.entities.entity_store.EntityStore` +
:class:`~app.entities.keep_store.KeepStore` protocols (+ the vocabulary provider for the effective
entity-like types, matching the node-delete path), so it unit-tests against fakes (no live DB/LLM —
08 testing policy).
"""

from __future__ import annotations

from ..config import Settings
from ..entities.entity_store import EntityStore
from ..entities.keep_store import KeepDecision, KeepStore, surface_forms
from ..vocab.service import VocabularyProvider, effective_vocabulary


class OrphanKeepError(Exception):
    """Base for keep-list problems surfaced to the API layer."""


class OrphanKeepNotFound(OrphanKeepError):
    """The node id is unknown or already a tombstone/deleted (404)."""


class OrphanKeepIsContent(OrphanKeepError):
    """The node is a **content** node, not an entity hub (400) — Keep is hubs-only (ADR-064 §5).
    Only a hub can be an intentionally-kept orphan; a content orphan is a capture artefact and is
    removed via capture-remove, not whitelisted."""


class OrphanKeepKeyNotFound(OrphanKeepError):
    """No keep exists for the given ``keep_key`` (404 on un-keep)."""


class OrphanKeepService:
    """Owns the synchronous keep / list / un-keep of orphan hubs (ADR-064 §5)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        keeps: KeepStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        self._keeps = keeps
        # Effective entity-like types (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds. Only
        # a hub of one of these types is keepable — a content node is rejected (like node-delete).
        self._vocab = vocab

    async def keep(self, node_id: str) -> KeepDecision:
        """Resolve the hub's current surface forms + type and upsert a durable keep (§5).

        Raises :class:`OrphanKeepNotFound` (unknown/tombstone) or :class:`OrphanKeepIsContent`
        (content node — Keep is hubs-only). Idempotent: re-keeping the same hub upserts the one
        decision. Returns the recorded decision (for the endpoint / tests)."""
        node = await self._entities.get_node(node_id)
        if node is None or node.merged_into is not None:
            raise OrphanKeepNotFound(node_id)
        entity_types = set(
            (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        )
        if node.type not in entity_types:
            raise OrphanKeepIsContent(node_id)
        decision = KeepDecision(
            node_type=node.type,
            forms=surface_forms(node.title, node.aliases),
            node_id=node.id,
        )
        await self._keeps.record(decision)
        return decision

    async def list_keeps(self) -> list[KeepDecision]:
        """Every recorded keep, newest first (the "Kept (N)" strip)."""
        return await self._keeps.all_keeps()

    async def unkeep(self, key: str) -> None:
        """Remove a keep by its stable ``keep_key``; raises :class:`OrphanKeepKeyNotFound` (404)
        when no such key exists. The hub reappears in the orphan check on the next run."""
        removed = await self._keeps.remove(key)
        if not removed:
            raise OrphanKeepKeyNotFound(key)
