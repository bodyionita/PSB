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

from fastapi import APIRouter, Depends, HTTPException, status

from ..chat.service import ChatAnswer, ChatService, ChatSessionNotFound
from ..dependencies import get_chat_service, get_model_routing, require_session
from ..models import (
    ChatMessageItem,
    ChatModelItem,
    ChatModelsResponse,
    ChatRequest,
    ChatResponse,
    ChatSessionDetail,
    ChatSessionItem,
    ChatSourceItem,
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
    )
