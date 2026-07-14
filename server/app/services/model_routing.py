"""Model routing — the UI-editable routing brain (ADR-025 / ADR-043, M4 task 1).

Three routing groups map every LLM chat call onto a ``{active, fallback, effort_by_provider}``
unit the user tunes from Settings:

  * ``chat``     — interactive chat (M4 task 3+).
  * ``conspect`` — organize/distill: capture organize, the follow-up nudge, chat query-condensation,
    tag/edge consolidation, entity resolution, profile generation (the 6 conspect call sites).
  * ``quick``    — a cheap/fast lane for trivial calls (ADR-043; M4 caller = session titling).

The **seed** for each group is config (:class:`~app.config.Settings` — ``chat_chain`` /
``distill_chain`` / ``quick_chain`` + ``claude_max_effort`` / ``quick_effort``); the user's saved
overrides live in ``app_settings`` under one ``model_routing`` jsonb key and are overlaid on top,
cache-busted on save (ADR-025 §3). The registry stays pure provider-mechanics — this service owns
*which* chain and *what* effort; :meth:`ProviderRegistry.run_chain` owns walking it (ADR-025 §3/§4).

Plain SQL over asyncpg, no ORM (rule 5, ADR-011). The service depends on the
:class:`ModelRoutingStore` protocol so it unit-tests against an in-memory fake (no live DB — 08
testing policy).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings
from ..db import Database
from ..providers.base import ChatMessage, ChatResult
from ..providers.registry import ChatModelOption, ProviderRegistry

logger = logging.getLogger(__name__)

# The routing groups this service governs (ADR-025 two → ADR-043 three).
GROUPS = ("chat", "conspect", "quick")

# The single ``app_settings`` key holding the saved per-group routing overrides.
MODEL_ROUTING_KEY = "model_routing"

# group → (settings chain attr, settings effort attr) — the config seed source per group.
_GROUP_SEED: dict[str, tuple[str, str]] = {
    "chat": ("chat_chain", "claude_max_effort"),
    "conspect": ("distill_chain", "claude_max_effort"),
    "quick": ("quick_chain", "quick_effort"),
}


@dataclass(frozen=True)
class GroupRouting:
    """One group's saved routing: the active model, its fallback, and per-provider effort.

    ``effort_by_provider`` maps a provider id → its reasoning effort; it only carries entries for
    providers that support one (the Claude tiers). Everything else is effort-irrelevant."""

    active: str = ""
    fallback: str = ""
    effort_by_provider: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingDecision:
    """A resolved group: the concrete provider chain to walk + the per-provider effort to thread."""

    chain: list[str]
    effort_by_provider: dict[str, str]


@dataclass(frozen=True)
class ChatCatalog:
    """The chat model picker payload (GET /chat/models, 03-api §Chat): every pickable chat model +
    the ``default`` = the resolved active model of the ``chat`` group (saved-over-seed)."""

    models: list[ChatModelOption]
    default: str


class ModelRoutingStore(Protocol):
    """Read/write the saved per-group routing overrides (the mutable half; seeds are config)."""

    async def get_all(self) -> dict[str, GroupRouting]:
        """The saved routing per group (only groups the user has saved; empty when unset)."""
        ...

    async def save(self, group: str, routing: GroupRouting) -> None:
        """Persist one group's routing (upsert into the ``model_routing`` jsonb; others left)."""
        ...


class PgModelRoutingStore:
    """asyncpg-backed routing store over ``app_settings`` — plain SQL (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_all(self) -> dict[str, GroupRouting]:
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT value FROM app_settings WHERE key = $1", MODEL_ROUTING_KEY
            )
        return _decode_all(value)

    async def save(self, group: str, routing: GroupRouting) -> None:
        if group not in GROUPS:
            raise ValueError(f"unknown routing group: {group!r}")
        # Read-modify-write in one transaction (single-user; app_settings is low-contention). The
        # row may not exist yet, so upsert with ON CONFLICT after merging the one group's entry.
        async with self._db.transaction() as conn:
            current = _decode_raw(
                await conn.fetchval(
                    "SELECT value FROM app_settings WHERE key = $1 FOR UPDATE", MODEL_ROUTING_KEY
                )
            )
            current[group] = _encode_group(routing)
            await conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = now()
                """,
                MODEL_ROUTING_KEY,
                json.dumps(current),
            )


class ModelRoutingService:
    """The routing brain (ADR-025 §3): resolve a group → chain + per-provider effort, then run it.

    Reads config seeds (settings) overlaid with saved overrides (``app_settings`` via the store),
    caching the saved half in memory and busting it on save so edits apply on the next request with
    no restart. A bad/stale saved model id degrades to the config seed chain, never a hard failure
    (rule 7 / ADR-025 §Consequences)."""

    def __init__(
        self, *, settings: Settings, store: ModelRoutingStore, registry: ProviderRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._cache: dict[str, GroupRouting] | None = None
        self._lock = asyncio.Lock()

    async def _saved(self) -> dict[str, GroupRouting]:
        if self._cache is None:
            async with self._lock:
                if self._cache is None:
                    self._cache = await self._store.get_all()
        return self._cache

    def invalidate(self) -> None:
        """Drop the in-memory cache so the next resolve re-reads ``app_settings`` (bust-on-save)."""
        self._cache = None

    def _seed(self, group: str) -> GroupRouting:
        """The config-declared default routing for a group (ADR-025 §3, ADR-043 §2)."""
        chain_attr, effort_attr = _GROUP_SEED[group]
        chain = list(getattr(self._settings, chain_attr))
        effort = getattr(self._settings, effort_attr)
        active = chain[0] if chain else ""
        fallback = chain[1] if len(chain) > 1 else ""
        ebp = {
            pid: effort
            for pid in _dedup([active, fallback])
            if pid and self._registry.supports_effort(pid)
        }
        return GroupRouting(active=active, fallback=fallback, effort_by_provider=ebp)

    async def chat_catalog(self) -> ChatCatalog:
        """The chat picker payload (GET /chat/models): all pickable chat models + the ``chat``
        group's resolved active model as ``default`` (saved-over-seed, rule-7 safe). Falls back to
        the registry's config default if the resolved chain is somehow empty."""
        decision = await self.resolve("chat")
        default = decision.chain[0] if decision.chain else self._registry.default_chat_model()
        return ChatCatalog(models=self._registry.chat_models(), default=default)

    async def resolve(self, group: str) -> RoutingDecision:
        """Resolve a group to a chain + per-provider effort (saved over seed; rule-7 safe)."""
        if group not in GROUPS:
            raise ValueError(f"unknown routing group: {group!r}")
        seed = self._seed(group)
        # Effort is read from whichever routing actually supplies the chain, so a rule-7 degrade
        # falls back to the config seed's effort too (not a stale saved value from junk routing).
        source = (await self._saved()).get(group) or seed
        chain = self._valid_chain(source.active, source.fallback)
        if not chain:
            # Saved routing points only at unknown/stale ids → fall back to the config seed.
            source = seed
            chain = self._valid_chain(seed.active, seed.fallback)
        effort = {
            pid: level
            for pid, level in source.effort_by_provider.items()
            if self._registry.supports_effort(pid)
        }
        return RoutingDecision(chain=chain, effort_by_provider=effort)

    def _valid_chain(self, active: str, fallback: str) -> list[str]:
        return [
            pid for pid in _dedup([active, fallback]) if pid and self._registry.supports_chat(pid)
        ]

    async def complete(
        self, group: str, messages: list[ChatMessage], *, requested_model: str | None = None
    ) -> ChatResult:
        """Route ``messages`` through ``group``'s resolved chain + effort (ADR-025).

        ``requested_model`` (the chat composer's per-conversation picker, ADR-025 §5) is tried
        first, the group's fallback + effort still applying underneath. Raises ``RegistryExhausted``
        when every provider in the resolved chain is unavailable (callers degrade as today)."""
        decision = await self.resolve(group)
        return await self._registry.run_chain(
            messages,
            chain=decision.chain,
            effort_by_provider=decision.effort_by_provider,
            requested_model=requested_model,
        )

    async def save(self, group: str, routing: GroupRouting) -> None:
        """Persist one group's routing and bust the cache (the PUT /settings/models write path)."""
        await self._store.save(group, routing)
        self.invalidate()


def build_model_routing(
    settings: Settings, db: Database, registry: ProviderRegistry
) -> ModelRoutingService:
    """Construct a routing service over the DB store — shared by ``main.py`` + the CLI factories."""
    return ModelRoutingService(settings=settings, store=PgModelRoutingStore(db), registry=registry)


def _dedup(values: list[str]) -> list[str]:
    """Order-preserving dedup (a group whose active == fallback walks that provider once)."""
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _decode_raw(value: Any) -> dict[str, Any]:
    """Decode the jsonb column to a plain dict (asyncpg returns jsonb as text)."""
    if value is None:
        return {}
    obj = json.loads(value) if isinstance(value, str) else dict(value)
    return obj if isinstance(obj, dict) else {}


def _decode_all(value: Any) -> dict[str, GroupRouting]:
    """Decode the ``model_routing`` jsonb into per-group :class:`GroupRouting` (skips junk)."""
    out: dict[str, GroupRouting] = {}
    for group, raw in _decode_raw(value).items():
        if group in GROUPS and isinstance(raw, dict):
            out[group] = _decode_group(raw)
    return out


def _decode_group(raw: dict[str, Any]) -> GroupRouting:
    active = raw.get("active")
    fallback = raw.get("fallback")
    ebp_raw = raw.get("effort_by_provider")
    ebp: dict[str, str] = {}
    if isinstance(ebp_raw, dict):
        for pid, level in ebp_raw.items():
            if isinstance(pid, str) and isinstance(level, str) and pid and level:
                ebp[pid] = level
    return GroupRouting(
        active=active if isinstance(active, str) else "",
        fallback=fallback if isinstance(fallback, str) else "",
        effort_by_provider=ebp,
    )


def _encode_group(routing: GroupRouting) -> dict[str, Any]:
    return {
        "active": routing.active,
        "fallback": routing.fallback,
        "effort_by_provider": dict(routing.effort_by_provider),
    }
