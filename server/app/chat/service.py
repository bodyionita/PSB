"""Chat service (M4 task 3, 04-pipelines §5, ADR-025/032/043).

One turn of grounded chat over the typed graph:

    persist user msg (BEFORE any model call — never-lose)
      → turn 1? raw msg : CONDENSE last N turns → standalone ENGLISH query (conspect group)
      → hybrid vector+FTS RRF retrieval (chat min_score floor — MINOR-1)
      → FENCE the numbered context as data-not-instructions → answer (chat group, picker override)
      → keep ONLY cited nodes, renumber [1..m]
      → persist assistant msg (model = resolved model, sources = cited nodes)
      → best-effort, non-blocking session titling (quick group) after the first exchange

Invariants: the user message is persisted before any model call (rule 2); every model call resolves
through the routing service so ``fallback_used`` is never swallowed (rule 3); titling runs in a
background task that can never fail the turn (rule 7). The answerer chain being fully down raises
``RegistryExhausted`` (the router maps it to 503) — the user turn is already durably persisted.

The service depends on the :class:`ChatStore` protocol + a small :class:`Retriever` protocol
(satisfied by :class:`~app.search.service.SearchService`), so it unit-tests against fakes with no
live DB/LLM (08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..config import Settings
from ..identity.store import IdentityCapsuleReader
from ..providers.base import ChatMessage, ProviderUnavailable
from ..search.store import SearchHit
from ..services.model_routing import ModelRoutingService
from .citations import renumber_citations
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    CONDENSE_SYSTEM_PROMPT,
    TITLE_SYSTEM_PROMPT,
    render_condense_input,
    render_context,
    render_identity,
    render_title_input,
)
from .store import ROLE_ASSISTANT, ROLE_USER, ChatMessageRecord, ChatSessionRecord, ChatStore

logger = logging.getLogger(__name__)


class Retriever(Protocol):
    """The retrieval surface the chat service needs — a subset of ``SearchService.search``."""

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        planes: list[str] | None = None,
        min_score: float | None = None,
        interiority_boost: float | None = None,
    ) -> list[SearchHit]: ...


class ChatError(Exception):
    """Base for chat problems surfaced to the API layer."""


class ChatSessionNotFound(ChatError):
    """A ``session_id`` was supplied that doesn't exist (404)."""


@dataclass(frozen=True)
class CitedSource:
    """A cited node in a chat answer (the API/persisted source shape, 03-api §Chat)."""

    node_id: str
    store_path: str
    type: str
    title: str | None
    snippet: str
    score: float
    planes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChatAnswer:
    """The result of one chat turn (POST /chat, task 4)."""

    session_id: str
    answer: str
    model_used: str
    fallback_used: bool
    effort_used: str | None = None
    sources: list[CitedSource] = field(default_factory=list)


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ChatStore,
        routing: ModelRoutingService,
        retriever: Retriever,
        capsule: IdentityCapsuleReader | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._routing = routing
        self._retriever = retriever
        # The identity capsule (M5 task 2, ADR-046 §5): a cheap last-distilled blob read prepended
        # to the system prompt for up-front grounding. Optional so the service builds without it.
        self._capsule = capsule
        # Strong refs to in-flight titling tasks so they aren't GC'd mid-run (drained on shutdown).
        self._tasks: set[asyncio.Task] = set()

    # --- public API ---------------------------------------------------------------------

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        planes: list[str] | None = None,
        top_k: int | None = None,
    ) -> ChatAnswer:
        """Run one chat turn and persist both messages (04 §5). Flow: see the module docstring."""
        session = await self._resolve_session(session_id)
        # History BEFORE we persist the new user turn: drives turn-1 detection + the condense/answer
        # window (bounded to the last N — 04 §5).
        history = await self._store.session_messages(
            session.id, limit=self._settings.chat_condense_last_n
        )
        # Never-lose (rule 2): the user's message is persisted before any model call.
        await self._store.add_message(session.id, role=ROLE_USER, content=message)

        query = message if not history else await self._condense(history, message)
        hits = await self._retrieve(query, planes=planes, top_k=top_k)

        result = await self._answer(history, message, hits, requested_model=model)
        answer_text, cited = renumber_citations(result.text, hits)
        sources = [_to_source(h) for h in cited]

        await self._store.add_message(
            session.id,
            role=ROLE_ASSISTANT,
            content=answer_text,
            model=result.model_used,
            sources=[_source_dict(s) for s in sources],
        )
        if result.model_used:
            await self._store.set_last_model(session.id, result.model_used)

        # Best-effort, non-blocking titling AFTER the first exchange (04 §5). `not history` marks
        # the first turn; `title is None` avoids re-titling a session that already has one.
        if not history and session.title is None:
            self._spawn(self._title_session(session.id, message, answer_text))

        return ChatAnswer(
            session_id=session.id,
            answer=answer_text,
            model_used=result.model_used,
            fallback_used=result.fallback_used,
            effort_used=result.effort_used,
            sources=sources,
        )

    async def list_sessions(self, *, limit: int | None = None) -> list[ChatSessionRecord]:
        """The thread list, newest-first (GET /chat/sessions). Bounded by
        ``chat_sessions_list_limit`` (03-api §Chat is unpaginated)."""
        return await self._store.list_sessions(limit or self._settings.chat_sessions_list_limit)

    async def get_session_detail(
        self, session_id: str
    ) -> tuple[ChatSessionRecord, list[ChatMessageRecord]]:
        """One session + its full message history, oldest-first (GET /chat/sessions/{id}). Raises
        ``ChatSessionNotFound`` (mapped to 404) when the id is unknown."""
        session = await self._store.get_session(session_id)
        if session is None:
            raise ChatSessionNotFound(session_id)
        messages = await self._store.session_messages(session_id)
        return session, messages

    async def drain(self) -> None:
        """Await any in-flight titling tasks (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- turn steps ---------------------------------------------------------------------

    async def _resolve_session(self, session_id: str | None):
        if session_id is not None:
            session = await self._store.get_session(session_id)
            if session is None:
                raise ChatSessionNotFound(session_id)
            return session
        new_id = await self._store.create_session()
        session = await self._store.get_session(new_id)
        if session is None:  # pragma: no cover — a row we just created must exist
            raise ChatError("failed to create chat session")
        return session

    async def _condense(self, history, message: str) -> str:
        """Rewrite the thread into a standalone ENGLISH query (conspect group, inherits its effort
        — 04 §5). Degrades to the raw message if the condense chain is down: the English corpus
        means a raw non-English query self-suppresses on the FTS leg while vectors stay
        cross-lingual, so retrieval still works (ADR-032)."""
        pairs = [(m.role, m.content) for m in history]
        messages = [
            ChatMessage(role="system", content=CONDENSE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=render_condense_input(pairs, message)),
        ]
        try:
            result = await self._routing.complete("conspect", messages)
        except ProviderUnavailable as exc:
            logger.info("chat condense chain unavailable, using raw message: %s", exc)
            return message
        condensed = result.text.strip()
        return condensed or message

    async def _retrieve(
        self, query: str, *, planes: list[str] | None, top_k: int | None
    ) -> list[SearchHit]:
        """Hybrid retrieval with the chat-tuned min_score floor (04 §5, MINOR-1). Best-effort: if
        the embedder is down (single provider, no hot fallback — ADR-022) the turn still answers,
        with no context (general questions uncited / "not in your memories" for personal ones)."""
        try:
            return await self._retriever.search(
                query,
                top_k=top_k,
                planes=planes,
                min_score=self._settings.chat_retrieval_min_score,
                interiority_boost=self._settings.chat_interiority_boost,
            )
        except ProviderUnavailable as exc:
            logger.warning("chat retrieval unavailable (answering without context): %s", exc)
            return []

    async def _answer(self, history, message: str, hits: list[SearchHit], *, requested_model):
        """Answer over the fenced numbered context (chat group; the picker's ``requested_model``
        overrides the active model, ADR-025 §5). The identity capsule (when present) is prepended as
        up-front grounding. Raises ``RegistryExhausted`` if the whole chat chain is down — the user
        turn is already persisted, and the router maps it to 503."""
        parts = [CHAT_SYSTEM_PROMPT]
        capsule = await self._identity_capsule()
        if capsule:
            parts.append(render_identity(capsule))
        # `now` is a live render (not a replayable pipeline), so wall-clock is correct here (rule 12
        # bars wall-clock only in replayable paths) — the temporal header/hints must be current.
        parts.append(render_context(hits, date.today()))
        messages = [ChatMessage(role="system", content="\n\n".join(parts))]
        messages.extend(ChatMessage(role=m.role, content=m.content) for m in history)
        messages.append(ChatMessage(role="user", content=message))
        return await self._routing.complete("chat", messages, requested_model=requested_model)

    async def _identity_capsule(self) -> str | None:
        """The last-distilled capsule text (ADR-046 §5), or ``None`` when absent / the read failed.
        Best-effort: a capsule read must never fail an answerable turn (rule 7)."""
        if self._capsule is None:
            return None
        try:
            blob = await self._capsule.current()
        except Exception:  # noqa: BLE001 — grounding is optional; answer without it
            logger.warning("chat: identity capsule read failed; answering without", exc_info=True)
            return None
        return blob.text if blob else None

    async def _title_session(self, session_id: str, user_message: str, answer: str) -> None:
        """Generate + save a short session title on the quick tier (ADR-043). Best-effort: any
        failure is logged, never propagated — the turn already returned (rule 7)."""
        try:
            result = await self._routing.complete(
                "quick",
                [
                    ChatMessage(role="system", content=TITLE_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=render_title_input(user_message, answer)),
                ],
            )
            title = _clean_title(result.text, self._settings.chat_title_max_chars)
            if title:
                await self._store.set_title(session_id, title)
        except ProviderUnavailable as exc:
            logger.info("session titling skipped (quick chain unavailable): %s", exc)
        except Exception:  # noqa: BLE001 — titling must never fail an answered turn (rule 7)
            logger.exception("session titling failed for %s (ignored)", session_id)

    # --- helpers ------------------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _to_source(hit: SearchHit) -> CitedSource:
    return CitedSource(
        node_id=hit.node_id,
        store_path=hit.store_path,
        type=hit.type,
        title=hit.title,
        snippet=hit.snippet,
        score=hit.score,
        planes=list(hit.planes),
    )


def _source_dict(source: CitedSource) -> dict:
    """The persisted/returned source shape (03-api §Chat): node_id, store_path, type, title,
    snippet, score, planes."""
    return {
        "node_id": source.node_id,
        "store_path": source.store_path,
        "type": source.type,
        "title": source.title,
        "snippet": source.snippet,
        "score": source.score,
        "planes": list(source.planes),
    }


def _clean_title(text: str, max_chars: int) -> str:
    """Strip a model title down to one clean line: drop surrounding quotes, collapse to the first
    line, and bound the length."""
    title = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(title) >= 2 and title[0] in "\"'" and title[-1] == title[0]:
        title = title[1:-1].strip()
    return title[:max_chars].strip()


def build_chat_service(
    settings: Settings,
    store: ChatStore,
    routing: ModelRoutingService,
    retriever: Retriever,
    capsule: IdentityCapsuleReader | None = None,
) -> ChatService:
    """Construct the chat service — shared by ``main.py`` (the router lands in task 4)."""
    return ChatService(
        settings=settings, store=store, routing=routing, retriever=retriever, capsule=capsule
    )
