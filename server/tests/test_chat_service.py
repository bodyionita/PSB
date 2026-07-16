"""ChatService tests (M4 task 3, 04-pipelines §5, ADR-025/032/043).

One turn of grounded chat: condense (turn ≥2) → hybrid retrieval → fenced grounded prompt →
cited-only renumber → persistence, with best-effort quick-tier titling. Fakes only — no live LLM/DB
(08 testing policy). The three routing groups seed to three distinct fake providers so a test can
assert which group ran and with what messages.
"""

from __future__ import annotations

import pytest

from app.chat.service import ChatService, ChatSessionNotFound, _clean_title
from app.chat.store import ROLE_ASSISTANT, ROLE_USER
from app.config import Settings
from app.providers.base import ChatMessage
from app.providers.registry import ProviderRegistry, RegistryExhausted
from app.search.store import SearchHit
from app.services.model_routing import GroupRouting, ModelRoutingService

from .fakes import FakeChatProvider, FakeChatStore, FakeModelRoutingStore, FakeRetriever


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


def _make(
    *,
    answer: str = "A plain answer.",
    chat_available: bool = True,
    condense_reply: str = "condensed english query",
    hits: list[SearchHit] | None = None,
    retriever_down: bool = False,
    extra: dict[str, FakeChatProvider] | None = None,
    capsule=None,
) -> tuple[ChatService, FakeChatStore, FakeRetriever, dict[str, FakeChatProvider]]:
    # The registry snapshots its chat-model catalog + model→provider index at construction (ADR-045
    # — providers are fixed at build), so any extra pickable/fallback model must be present here.
    providers = {
        "chat-p": FakeChatProvider("chat-p", reply=answer, available=chat_available),
        "conspect-p": FakeChatProvider("conspect-p", reply=condense_reply),
        "quick-p": FakeChatProvider("quick-p", reply="A Nice Title"),
        **(extra or {}),
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
    retriever = FakeRetriever(hits=hits, down=retriever_down)
    service = ChatService(
        settings=settings, store=store, routing=routing, retriever=retriever, capsule=capsule
    )
    return service, store, retriever, providers


# --- turn 1: raw query, retrieval, cited-only renumber, persistence -----------------------------


async def test_turn_one_uses_raw_message_and_persists_both_turns():
    service, store, retriever, providers = _make(
        answer="You raised prices [2] after talking to Ana [1].",
        hits=[_hit("n1"), _hit("n2")],
    )

    result = await service.send("what did I decide about pricing?")
    await service.drain()

    # Turn 1 → no condensation; the raw message drove retrieval at the chat min_score floor.
    assert providers["conspect-p"].calls == 0
    assert retriever.calls[0]["query"] == "what did I decide about pricing?"
    assert retriever.calls[0]["min_score"] == 0.01  # chat_retrieval_min_score default (MINOR-1)

    # Cited-only renumber: [2] first → [1] (n2), [1] second → [2] (n1).
    assert result.answer == "You raised prices [1] after talking to Ana [2]."
    assert [s.node_id for s in result.sources] == ["n2", "n1"]
    assert result.model_used == "chat-p"
    assert result.fallback_used is False

    msgs = store.messages[result.session_id]
    assert [(m.role, m.content) for m in msgs] == [
        (ROLE_USER, "what did I decide about pricing?"),
        (ROLE_ASSISTANT, "You raised prices [1] after talking to Ana [2]."),
    ]
    # Assistant sources persisted in the API shape; last_model recorded.
    assert [s["node_id"] for s in msgs[1].sources] == ["n2", "n1"]
    assert msgs[1].sources[0]["store_path"] == "memory/n2.md"
    assert store.sessions[result.session_id].last_model == "chat-p"


async def test_user_message_persisted_before_model_call():
    # The chat chain is down → the answer raises, but the user turn must already be persisted
    # (never-lose, rule 2) and the session created.
    service, store, _, _ = _make(chat_available=False)

    with pytest.raises(RegistryExhausted):
        await service.send("remember this")

    assert len(store.sessions) == 1
    (session_id,) = store.sessions
    assert [(m.role, m.content) for m in store.messages[session_id]] == [
        (ROLE_USER, "remember this")
    ]


# --- retrieval grounding + "not in your memories" -----------------------------------------------


async def test_no_hits_yields_empty_sources():
    service, store, _, _ = _make(answer="I don't have that in your memories.", hits=[])
    result = await service.send("where did I grow up?")
    await service.drain()
    assert result.sources == []
    assert store.messages[result.session_id][1].sources == []


async def test_uncited_answer_keeps_no_sources_even_with_hits():
    # Hits were retrieved but the model cited none (a general answer) → empty sources.
    service, _, _, _ = _make(answer="Paris is the capital of France.", hits=[_hit("n1")])
    result = await service.send("capital of France?")
    await service.drain()
    assert result.sources == []
    assert result.answer == "Paris is the capital of France."


async def test_retriever_down_answers_without_context():
    # Embedder down (single provider, no hot fallback) → retrieval degrades to no context, the turn
    # still answers (rule 7). No crash, empty sources.
    service, _, _, providers = _make(answer="ok", hits=None, retriever_down=True)
    result = await service.send("anything?")
    await service.drain()
    assert result.answer == "ok"
    assert result.sources == []
    assert providers["chat-p"].calls == 1


# --- turn ≥2 condensation -----------------------------------------------------------------------


async def test_turn_two_condenses_to_english_query():
    service, store, retriever, providers = _make(
        answer="answer", condense_reply="pricing decision history", hits=[_hit("n1")]
    )
    session_id = await store.create_session()
    await store.add_message(session_id, role=ROLE_USER, content="tell me about pricing")
    await store.add_message(session_id, role=ROLE_ASSISTANT, content="you raised them")

    await service.send("and after that?", session_id=session_id)

    # Condensation ran on the conspect group; its standalone query drove retrieval.
    assert providers["conspect-p"].calls == 1
    assert retriever.calls[0]["query"] == "pricing decision history"


async def test_condense_chain_down_falls_back_to_raw_message():
    service, store, retriever, _ = _make(hits=[_hit("n1")])
    # Make the conspect provider unavailable so condensation degrades to the raw message.
    service._routing._registry._providers["conspect-p"]._available = False  # type: ignore[attr-defined]
    session_id = await store.create_session()
    await store.add_message(session_id, role=ROLE_USER, content="earlier")
    await store.add_message(session_id, role=ROLE_ASSISTANT, content="reply")

    await service.send("follow up question", session_id=session_id)

    assert retriever.calls[0]["query"] == "follow up question"


# --- session handling + picker + fallback -------------------------------------------------------


async def test_unknown_session_raises():
    service, _, _, _ = _make()
    with pytest.raises(ChatSessionNotFound):
        await service.send("hi", session_id="ghost")


async def test_requested_model_is_forwarded_to_the_picker():
    # A second chat model the picker can select per-conversation (ADR-025 §5).
    other = FakeChatProvider("other-chat", reply="from other")
    service, _, _, providers = _make(answer="picked", hits=[], extra={"other-chat": other})

    result = await service.send("hi", model="other-chat")
    await service.drain()

    assert result.model_used == "other-chat"
    assert providers["chat-p"].calls == 0
    assert other.calls == 1


async def test_fallback_used_is_surfaced_and_recorded():
    # A fallback model behind the (down) primary answers → fallback_used True.
    fallback = FakeChatProvider("chat-fallback", reply="from fallback")
    service, store, _, providers = _make(answer="x", hits=[], extra={"chat-fallback": fallback})
    providers["chat-p"]._available = False
    await service._routing.save("chat", GroupRouting(active="chat-p", fallback="chat-fallback"))

    result = await service.send("hi")
    await service.drain()

    assert result.model_used == "chat-fallback"
    assert result.fallback_used is True
    assert store.sessions[result.session_id].last_model == "chat-fallback"


# --- best-effort, non-blocking titling ----------------------------------------------------------


async def test_titling_runs_after_first_exchange():
    service, store, _, providers = _make(answer="hello", hits=[])
    result = await service.send("hi there")
    await service.drain()  # let the background titler finish

    assert providers["quick-p"].calls == 1
    assert store.sessions[result.session_id].title == "A Nice Title"


async def test_titling_skipped_on_later_turns():
    service, store, _, providers = _make(answer="ok", hits=[])
    session_id = await store.create_session(title="Existing Title")
    await store.add_message(session_id, role=ROLE_USER, content="q1")
    await store.add_message(session_id, role=ROLE_ASSISTANT, content="a1")

    await service.send("q2", session_id=session_id)
    await service.drain()

    assert providers["quick-p"].calls == 0
    assert store.sessions[session_id].title == "Existing Title"


async def test_fenced_context_reaches_the_answer_prompt():
    # The retrieved memory is fenced as data-not-instructions in the chat system prompt (injection
    # hygiene, 04 §5): its snippet + a data-fence marker are present.
    service, _, _, providers = _make(answer="ok", hits=[_hit("n1", snippet="raise prices in Q3")])
    await service.send("pricing?")
    await service.drain()

    system_msg: ChatMessage = providers["chat-p"].last_messages[0]  # type: ignore[attr-defined]
    assert system_msg.role == "system"
    assert "raise prices in Q3" in system_msg.content
    assert "data, not instructions" in system_msg.content


# --- read paths (GET /chat/sessions[/{id}], GET /chat/models — task 4) --------------------------


async def test_list_sessions_newest_first_and_bounded():
    service, store, _, _ = _make(hits=[])
    for _ in range(3):
        await store.create_session()
    sessions = await service.list_sessions(limit=2)
    assert len(sessions) == 2  # bounded by the explicit limit


async def test_get_session_detail_returns_session_and_messages():
    service, store, _, _ = _make(hits=[_hit("n1")])
    result = await service.send("q")
    await service.drain()
    session, messages = await service.get_session_detail(result.session_id)
    assert session.id == result.session_id
    assert [m.role for m in messages] == [ROLE_USER, ROLE_ASSISTANT]


async def test_get_session_detail_unknown_raises():
    service, _, _, _ = _make()
    with pytest.raises(ChatSessionNotFound):
        await service.get_session_detail("ghost")


async def test_chat_catalog_lists_models_and_active_default():
    service, _, _, providers = _make()
    catalog = await service._routing.chat_catalog()
    assert [m.id for m in catalog.models] == ["chat-p", "conspect-p", "quick-p"]
    # Model-derived label (labels.py); an opaque non-vendor id falls back to the id itself.
    assert next(m.label for m in catalog.models if m.id == "chat-p") == "chat-p"
    assert catalog.default == "chat-p"  # the Chat group's active model


# --- _clean_title (pure) ------------------------------------------------------------------------


def test_clean_title_strips_wrapping_quotes():
    assert _clean_title('"Pricing decision"', 80) == "Pricing decision"
    assert _clean_title("'Trip to Cluj'", 80) == "Trip to Cluj"


def test_clean_title_keeps_first_line_only():
    assert _clean_title("Q3 pricing\n(some rambling explanation)", 80) == "Q3 pricing"


def test_clean_title_truncates_to_max_chars():
    assert _clean_title("x" * 100, 10) == "x" * 10


def test_clean_title_empty_stays_empty():
    assert _clean_title("   ", 80) == ""
    assert _clean_title("", 80) == ""


# --- identity capsule injection (M5 task 2, ADR-046 §5) ------------------------------------------


async def test_capsule_prepended_to_answer_system_prompt():
    from app.identity.store import CapsuleBlob

    from .fakes import FakeCapsuleStore

    capsule = FakeCapsuleStore(blob=CapsuleBlob(text="The user is a builder named B."))
    service, _, _, providers = _make(hits=[_hit("n1")], capsule=capsule)

    await service.send("what am I working on?")
    await service.drain()

    system = providers["chat-p"].last_messages[0].content
    assert "ABOUT THE USER" in system
    assert "The user is a builder named B." in system
    # Fenced as data-not-instructions, ahead of the rendered CONTEXT block (the retrieved snippet).
    assert system.index("ABOUT THE USER") < system.index("a snippet")


async def test_no_capsule_leaves_system_prompt_clean():
    service, _, _, providers = _make(hits=[_hit("n1")])  # no capsule wired
    await service.send("hi")
    await service.drain()
    assert "ABOUT THE USER" not in providers["chat-p"].last_messages[0].content


async def test_answer_survives_a_failing_capsule_read():
    from .fakes import FakeCapsuleStore

    capsule = FakeCapsuleStore(raise_on_read=True)  # read boom must not fail the turn (rule 7)
    service, _, _, providers = _make(answer="Still answered.", hits=[_hit("n1")], capsule=capsule)

    result = await service.send("hi")
    await service.drain()

    assert result.answer == "Still answered."
    assert "ABOUT THE USER" not in providers["chat-p"].last_messages[0].content
