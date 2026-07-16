"""Chat router tests (M4 task 4, 03-api §Chat): POST /chat + GET /chat/models + GET
/chat/sessions[/{id}], error mapping (RegistryExhausted→503, unknown session→404). A real
ChatService + ModelRoutingService over fakes drive the HTTP layer — no DB, no LLM, auth bypassed.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat.auto_recorded import AutoRecordedItem, AutoRecordNotFound
from app.chat.distiller import RememberOutcome, SessionNotFound
from app.chat.service import ChatService
from app.config import Settings
from app.dependencies import (
    get_auto_recorded_service,
    get_chat_distiller_service,
    get_chat_service,
    get_model_routing,
    require_session,
)
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


# --- POST /chat/sessions/{id}/remember (M6 task 3, ADR-048 §6) ------------------------------------


class _StubDistiller:
    """A stand-in ChatDistillerService for the router's HTTP mapping — the pass logic is covered in
    test_chat_distiller; here we only assert outcome → response + error mapping."""

    def __init__(self, *, outcome: RememberOutcome | None = None, not_found: bool = False) -> None:
        self._outcome = outcome
        self._not_found = not_found
        self.calls: list[str] = []

    async def remember(self, session_id: str) -> RememberOutcome:
        self.calls.append(session_id)
        if self._not_found:
            raise SessionNotFound(session_id)
        assert self._outcome is not None
        return self._outcome


def _remember_client(distiller: _StubDistiller) -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router, prefix=PREFIX)
    app.dependency_overrides[get_chat_distiller_service] = lambda: distiller
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_remember_returns_counts_when_the_pass_ran():
    distiller = _StubDistiller(outcome=RememberOutcome(endorsed=2, to_review=1))
    sid = str(uuid.uuid4())
    resp = _remember_client(distiller).post(f"{PREFIX}/chat/sessions/{sid}/remember")
    assert resp.status_code == 200
    assert resp.json() == {"endorsed": 2, "to_review": 1, "skipped": None}
    assert distiller.calls == [sid]


def test_remember_returns_skipped_reason_on_noop():
    distiller = _StubDistiller(
        outcome=RememberOutcome(endorsed=0, to_review=0, skipped_reason="no new messages")
    )
    resp = _remember_client(distiller).post(f"{PREFIX}/chat/sessions/{uuid.uuid4()}/remember")
    assert resp.status_code == 200
    assert resp.json() == {"endorsed": None, "to_review": None, "skipped": "no new messages"}


def test_remember_unknown_session_is_404():
    resp = _remember_client(_StubDistiller(not_found=True)).post(
        f"{PREFIX}/chat/sessions/{uuid.uuid4()}/remember"
    )
    assert resp.status_code == 404


def test_remember_malformed_session_id_is_422():
    distiller = _StubDistiller(outcome=RememberOutcome(endorsed=0, to_review=0))
    resp = _remember_client(distiller).post(f"{PREFIX}/chat/sessions/not-a-uuid/remember")
    assert resp.status_code == 422
    assert distiller.calls == []  # never reached the service


# --- GET /chat/auto-recorded + POST .../remove (M6 task 4, ADR-048 §11/§12) ----------------------


class _StubAutoRecorded:
    """A stand-in AutoRecordedService for the router's HTTP mapping — the list/remove logic is
    covered in test_auto_recorded; here we only assert wire shape + error/limit mapping."""

    def __init__(
        self, *, items: list[AutoRecordedItem] | None = None, not_found: bool = False
    ) -> None:
        self._items = items or []
        self._not_found = not_found
        self.list_calls: list[int | None] = []
        self.remove_calls: list[str] = []

    async def list_recent(self, limit: int | None = None) -> list[AutoRecordedItem]:
        self.list_calls.append(limit)
        return self._items

    async def remove(self, capture_id: str) -> None:
        self.remove_calls.append(capture_id)
        if self._not_found:
            raise AutoRecordNotFound(capture_id)


def _auto_client(service: _StubAutoRecorded) -> TestClient:
    app = FastAPI()
    app.include_router(chat_router.router, prefix=PREFIX)
    app.dependency_overrides[get_auto_recorded_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def _item(cid: str) -> AutoRecordedItem:
    return AutoRecordedItem(
        capture_id=cid,
        node_paths=[f"memory/2026-07-16--m--{cid}.md"],
        title="The user decided X",
        snippet="The user decided X.",
        salience="high",
        source_ref="sess-1",
        created_at=None,
    )


def test_get_auto_recorded_lists_items_and_forwards_limit():
    stub = _StubAutoRecorded(items=[_item("c1"), _item("c2")])
    resp = _auto_client(stub).get(f"{PREFIX}/chat/auto-recorded?limit=25")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["capture_id"] for i in body] == ["c1", "c2"]
    assert body[0]["salience"] == "high"
    assert body[0]["node_paths"] == ["memory/2026-07-16--m--c1.md"]
    assert stub.list_calls == [25]


def test_get_auto_recorded_rejects_out_of_range_limit():
    stub = _StubAutoRecorded()
    assert _auto_client(stub).get(f"{PREFIX}/chat/auto-recorded?limit=0").status_code == 422
    assert _auto_client(stub).get(f"{PREFIX}/chat/auto-recorded?limit=99999").status_code == 422


def test_remove_auto_recorded_returns_204():
    stub = _StubAutoRecorded()
    cid = str(uuid.uuid4())
    resp = _auto_client(stub).post(f"{PREFIX}/chat/auto-recorded/{cid}/remove")
    assert resp.status_code == 204
    assert stub.remove_calls == [cid]


def test_remove_auto_recorded_unknown_is_404():
    stub = _StubAutoRecorded(not_found=True)
    resp = _auto_client(stub).post(f"{PREFIX}/chat/auto-recorded/{uuid.uuid4()}/remove")
    assert resp.status_code == 404


def test_remove_auto_recorded_malformed_id_is_422():
    stub = _StubAutoRecorded()
    resp = _auto_client(stub).post(f"{PREFIX}/chat/auto-recorded/not-a-uuid/remove")
    assert resp.status_code == 422
    assert stub.remove_calls == []  # never reached the service


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
    # Labels are model-derived (labels.py); an opaque non-vendor id falls back to the id itself.
    # The friendly-label path over real vendor ids is covered in test_labels + the registry/settings
    # tests. Here the ids double as model ids (one-model fakes), so labels equal the ids.
    assert labels["chat-p"] == "chat-p"
    assert labels["conspect-p"] == "conspect-p"
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
