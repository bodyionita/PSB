"""Chat router tests (M4 task 4, 03-api §Chat): POST /chat + GET /chat/models + GET
/chat/sessions[/{id}], error mapping (RegistryExhausted→503, unknown session→404). A real
ChatService + ModelRoutingService over fakes drive the HTTP layer — no DB, no LLM, auth bypassed.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat.service import ChatService
from app.config import Settings
from app.dependencies import get_chat_service, get_model_routing, require_session
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ProviderRegistry
from app.routers import chat as chat_router
from app.search.store import SearchHit
from app.services.model_routing import ModelRoutingService

from .fakes import FakeChatProvider, FakeChatStore, FakeModelRoutingStore, FakeRetriever

PREFIX = "/api/v1"


def _hit(node_id: str, *, snippet: str = "a snippet", score: float = 0.03) -> SearchHit:
    return SearchHit(
        node_id=node_id,
        store_path=f"memory/{node_id}.md",
        type="memory",
        title=f"Title {node_id}",
        plane="Ideas",
        planes=["Ideas"],
        tags=["t"],
        snippet=snippet,
        score=score,
    )


def _build(
    *,
    answer: str = "A plain answer.",
    chat_available: bool = True,
    hits: list[SearchHit] | None = None,
) -> tuple[ChatService, ModelRoutingService, FakeChatStore]:
    chat_p = FakeChatProvider("chat-p", reply=answer, available=chat_available)
    chat_p.label = "Claude Opus 4.8"  # provider-sourced label (GET /chat/models)
    providers = {
        "chat-p": chat_p,
        "conspect-p": FakeChatProvider("conspect-p", reply="condensed english query"),
        "quick-p": FakeChatProvider("quick-p", reply="A Nice Title"),
        # An STT/embedding-only instance: a ChatProvider by class but can_chat False, so it must
        # NOT appear in GET /chat/models (regression guard for the chat-capability filter).
        "stt-only": OpenAICompatibleProvider(id="stt-only", base_url="x", api_key="k"),
    }
    registry = ProviderRegistry(
        providers,
        chat_chain=["chat-p"],
        distill_chain=["conspect-p"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    settings = Settings(
        chat_chain=["chat-p"], distill_chain=["conspect-p"], quick_chain=["quick-p"]
    )
    routing = ModelRoutingService(
        settings=settings, store=FakeModelRoutingStore(), registry=registry
    )
    store = FakeChatStore()
    service = ChatService(
        settings=settings, store=store, routing=routing, retriever=FakeRetriever(hits=hits)
    )
    return service, routing, store


def _client(service: ChatService, routing: ModelRoutingService) -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router, prefix=PREFIX)
    app.dependency_overrides[get_chat_service] = lambda: service
    app.dependency_overrides[get_model_routing] = lambda: routing
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


# --- POST /chat -----------------------------------------------------------------------------------


def test_post_chat_answers_with_renumbered_citations_and_creates_session():
    service, routing, _ = _build(
        answer="You raised prices [2] after talking to Ana [1].", hits=[_hit("n1"), _hit("n2")]
    )
    resp = _client(service, routing).post(
        f"{PREFIX}/chat", json={"message": "what did I decide about pricing?"}
    )
    assert resp.status_code == 200
    body = resp.json()
    # Implicit session creation → a session_id is returned.
    assert body["session_id"]
    # Cited-only renumber ([2]→[1] n2, [1]→[2] n1) + source shape.
    assert body["answer"] == "You raised prices [1] after talking to Ana [2]."
    assert [s["node_id"] for s in body["sources"]] == ["n2", "n1"]
    assert body["sources"][0]["planes"] == ["Ideas"]
    assert body["model_used"] == "chat-p"
    assert body["fallback_used"] is False


def test_post_chat_forwards_picker_and_filters():
    service, routing, _ = _build(hits=[_hit("n1")])
    resp = _client(service, routing).post(
        f"{PREFIX}/chat",
        json={"message": "hi", "model": "chat-p", "planes": ["Ideas"], "top_k": 3},
    )
    assert resp.status_code == 200


def test_post_chat_continues_existing_session():
    service, routing, store = _build(answer="Second answer.", hits=[])
    sid = None
    client = _client(service, routing)
    first = client.post(f"{PREFIX}/chat", json={"message": "first"})
    sid = first.json()["session_id"]
    second = client.post(f"{PREFIX}/chat", json={"message": "second", "session_id": sid})
    assert second.status_code == 200
    assert second.json()["session_id"] == sid
    # Both turns of both exchanges persisted under the one session.
    assert len(store.messages[sid]) == 4


def test_post_chat_unknown_session_is_404():
    service, routing, _ = _build()
    resp = _client(service, routing).post(
        f"{PREFIX}/chat", json={"message": "hi", "session_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


def test_post_chat_malformed_session_id_is_422():
    # A non-uuid session_id is rejected at the boundary (422), never reaching the uuid DB column.
    service, routing, _ = _build()
    resp = _client(service, routing).post(
        f"{PREFIX}/chat", json={"message": "hi", "session_id": "not-a-uuid"}
    )
    assert resp.status_code == 422


def test_post_chat_all_models_down_is_503():
    service, routing, store = _build(chat_available=False)
    resp = _client(service, routing).post(f"{PREFIX}/chat", json={"message": "hi"})
    assert resp.status_code == 503
    # Never-lose (rule 2): the user turn is durably persisted even though the answer chain failed.
    [(sid, msgs)] = store.messages.items()
    assert [m.role for m in msgs] == ["user"]


def test_post_chat_empty_message_is_422():
    service, routing, _ = _build()
    resp = _client(service, routing).post(f"{PREFIX}/chat", json={"message": ""})
    assert resp.status_code == 422


# --- GET /chat/models -----------------------------------------------------------------------------


def test_get_chat_models_lists_registry_ids_labels_and_default():
    service, routing, _ = _build()
    resp = _client(service, routing).get(f"{PREFIX}/chat/models")
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["id"] for m in body["models"]]
    # Only genuinely chat-capable providers; the STT/embedding-only instance is excluded.
    assert ids == ["chat-p", "conspect-p", "quick-p"]
    assert "stt-only" not in ids
    labels = {m["id"]: m["label"] for m in body["models"]}
    assert labels["chat-p"] == "Claude Opus 4.8"  # provider-sourced label
    assert labels["conspect-p"] == "conspect-p"  # falls back to id when unset
    assert body["default"] == "chat-p"  # the Chat group's active model


# --- GET /chat/sessions[/{id}] --------------------------------------------------------------------


def test_get_sessions_lists_newest_first():
    service, routing, store = _build(hits=[])
    client = _client(service, routing)
    client.post(f"{PREFIX}/chat", json={"message": "one"})
    client.post(f"{PREFIX}/chat", json={"message": "two"})
    resp = client.get(f"{PREFIX}/chat/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 2
    assert {"id", "title", "created_at", "last_model"} <= set(sessions[0].keys())
    assert all(s["last_model"] == "chat-p" for s in sessions)


def test_get_session_detail_returns_messages_with_sources():
    service, routing, _ = _build(answer="Answer citing [1].", hits=[_hit("n1")])
    client = _client(service, routing)
    sid = client.post(f"{PREFIX}/chat", json={"message": "q"}).json()["session_id"]
    resp = client.get(f"{PREFIX}/chat/sessions/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == sid
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assistant = body["messages"][1]
    assert assistant["model"] == "chat-p"
    assert [s["node_id"] for s in assistant["sources"]] == ["n1"]
    # The user turn carries no sources.
    assert body["messages"][0]["sources"] == []


def test_get_session_detail_unknown_is_404():
    service, routing, _ = _build()
    resp = _client(service, routing).get(f"{PREFIX}/chat/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_session_detail_malformed_id_is_422():
    service, routing, _ = _build()
    resp = _client(service, routing).get(f"{PREFIX}/chat/sessions/not-a-uuid")
    assert resp.status_code == 422
