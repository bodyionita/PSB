"""Chat router (03-api.md §Chat, M4 / ADR-025).

Thin HTTP surface over :class:`ChatService` (CLAUDE.md rule 5 — routers validate + delegate); the
condense → hybrid-retrieval → grounded-answer → persistence logic all lives in the service (task 3).
Session-gated like every non-public route.

``POST /chat`` runs one grounded turn (implicit session creation; the composer's ``model`` overrides
the Chat group's active model, ADR-025 §5). The whole chat chain being down surfaces as
:class:`RegistryExhausted` → ``503`` (the user turn is already durably persisted — rule 2); a
well-formed but unknown ``session_id`` is :class:`ChatSessionNotFound` → ``404`` (a malformed id
is a ``422`` — session ids are uuids, validated at the boundary so the store never sees a bad one).
``GET /chat/models`` feeds the picker; ``GET /chat/sessions[/{id}]`` are read-only views.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..chat.auto_recorded import AutoRecordedService, AutoRecordNotFound
from ..chat.distiller import ChatDistillerService, SessionNotFound
from ..chat.service import ChatAnswer, ChatService, ChatSessionNotFound
from ..dependencies import (
    get_auto_recorded_service,
    get_chat_distiller_service,
    get_chat_service,
    get_model_routing,
    require_session,
)
from ..models import (
    AutoRecordedItem,
    ChatMessageItem,
    ChatModelItem,
    ChatModelsResponse,
    ChatRequest,
    ChatResponse,
    ChatSessionDetail,
    ChatSessionItem,
    ChatSourceItem,
    RememberResponse,
)
from ..providers.registry import RegistryExhausted
from ..services.model_routing import ModelRoutingService

router = APIRouter(tags=["chat"], dependencies=[Depends(require_session)])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    try:
        answer = await service.send(
            payload.message,
            session_id=str(payload.session_id) if payload.session_id else None,
            model=payload.model,
            planes=payload.planes,
            top_k=payload.top_k,
        )
    except ChatSessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="chat session not found"
        ) from None
    except RegistryExhausted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chat is temporarily unavailable (all models)",
        ) from None
    return _to_response(answer)


@router.get("/chat/models", response_model=ChatModelsResponse)
async def chat_models(
    routing: ModelRoutingService = Depends(get_model_routing),
) -> ChatModelsResponse:
    catalog = await routing.chat_catalog()
    return ChatModelsResponse(
        models=[
            ChatModelItem(id=m.id, label=m.label, effort=catalog.efforts.get(m.id))
            for m in catalog.models
        ],
        default=catalog.default,
    )


@router.get("/chat/sessions", response_model=list[ChatSessionItem])
async def list_sessions(
    service: ChatService = Depends(get_chat_service),
) -> list[ChatSessionItem]:
    sessions = await service.list_sessions()
    return [
        ChatSessionItem(id=s.id, title=s.title, created_at=s.created_at, last_model=s.last_model)
        for s in sessions
    ]


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_session(
    session_id: uuid.UUID,
    service: ChatService = Depends(get_chat_service),
) -> ChatSessionDetail:
    # uuid path type → 422 on a malformed id (consistent with GET /nodes/{id}); a well-formed but
    # unknown id resolves to 404 below. The store only ever receives a valid uuid string.
    try:
        session, messages = await service.get_session_detail(str(session_id))
    except ChatSessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="chat session not found"
        ) from None
    return ChatSessionDetail(
        id=session.id,
        title=session.title,
        messages=[
            ChatMessageItem(
                role=m.role,
                content=m.content,
                model=m.model,
                sources=[_source_item(s) for s in m.sources],
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


@router.post("/chat/sessions/{session_id}/remember", response_model=RememberResponse)
async def remember_session(
    session_id: uuid.UUID,
    distiller: ChatDistillerService = Depends(get_chat_distiller_service),
) -> RememberResponse:
    """Distill this session now (M6, ADR-048 §6): the **same** single distill pass, synchronously,
    on the delta-after-watermark — same salience + stance gate (no force-endorse), advancing the
    same watermark so it stays idempotent with the nightly run. Endorsed candidates organize in the
    background. Returns the ``{endorsed, to_review}`` counts, or ``{skipped}`` when there is nothing
    new past the watermark / the model chain is down. ``422`` malformed id; ``404`` unknown session.
    """
    try:
        outcome = await distiller.remember(str(session_id))
    except SessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="chat session not found"
        ) from None
    if outcome.skipped:
        return RememberResponse(skipped=outcome.skipped_reason)
    return RememberResponse(endorsed=outcome.endorsed, to_review=outcome.to_review)


@router.get("/chat/auto-recorded", response_model=list[AutoRecordedItem])
async def list_auto_recorded(
    limit: int = Query(default=50, ge=1, le=500),
    service: AutoRecordedService = Depends(get_auto_recorded_service),
) -> list[AutoRecordedItem]:
    """The chat-scoped "recently auto-recorded" audit list (M6, ADR-048 §12): auto-endorsed chat
    memories newest-first, feeding the one-tap-remove surface. Bounded by ``limit`` (config-capped).
    """
    items = await service.list_recent(limit)
    return [
        AutoRecordedItem(
            capture_id=i.capture_id,
            node_paths=i.node_paths,
            title=i.title,
            snippet=i.snippet,
            salience=i.salience,
            source_ref=i.source_ref,
            created_at=i.created_at,
        )
        for i in items
    ]


@router.post("/chat/auto-recorded/{capture_id}/remove", status_code=status.HTTP_204_NO_CONTENT)
async def remove_auto_recorded(
    capture_id: uuid.UUID,
    service: AutoRecordedService = Depends(get_auto_recorded_service),
) -> None:
    """One-tap remove of an auto-endorsed chat memory (M6, ADR-048 §11): git-rm the node file(s) +
    DB-delete (``nodes``/``chunks``/``edges``) + tombstone the capture (``removed_at``, replay-
    excluded so it can't resurrect). Soft-delete — git history is kept. ``422`` malformed id;
    ``404`` for an id that is not a live auto-recorded item (unknown / already removed / not
    auto-endorsed)."""
    try:
        await service.remove(str(capture_id))
    except AutoRecordNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="auto-recorded memory not found"
        ) from None


def _to_response(answer: ChatAnswer) -> ChatResponse:
    return ChatResponse(
        session_id=answer.session_id,
        answer=answer.answer,
        model_used=answer.model_used,
        fallback_used=answer.fallback_used,
        effort_used=answer.effort_used,
        sources=[
            ChatSourceItem(
                node_id=s.node_id,
                store_path=s.store_path,
                type=s.type,
                title=s.title,
                snippet=s.snippet,
                score=s.score,
                planes=s.planes,
                media_kinds=s.media_kinds,
            )
            for s in answer.sources
        ],
    )


def _source_item(source: dict) -> ChatSourceItem:
    """A persisted source (jsonb dict in the API shape) → wire item (GET /chat/sessions/{id})."""
    return ChatSourceItem(
        node_id=source["node_id"],
        store_path=source["store_path"],
        type=source["type"],
        title=source.get("title"),
        snippet=source["snippet"],
        score=source["score"],
        planes=source.get("planes", []),
        # Persisted history predating M9 T4 has no `media_kinds` — default to empty (ADR-060 §7).
        media_kinds=source.get("media_kinds", []),
    )
