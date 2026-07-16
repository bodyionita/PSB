"""Chat-distiller tests (M6 task 1, ADR-048) — stance-gated distillation of idle chat sessions.

Pure helpers (parse / fence / hedge / anchor) are unit-tested directly; the service is exercised
against fakes (distill store, capture-ingest, review queue, run store) + a routing over a fake
`conspect` provider that returns a scripted distill JSON — no live LLM/DB (08 testing policy).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.chat.distill_store import DistillableSession
from app.chat.distiller import (
    AGENT,
    ChatDistillerService,
    SessionNotFound,
    _anchor_time,
    _has_hedge,
    parse_distill_candidates,
    render_distill_input,
)
from app.chat.store import ChatMessageRecord
from app.config import Settings
from app.providers.registry import ProviderRegistry
from app.services.model_routing import ModelRoutingService
from app.services.review_queue import KIND_STANCE_CANDIDATE

from .fakes import (
    FakeAgentRunStore,
    FakeChatCaptureIngest,
    FakeChatDistillStore,
    FakeChatProvider,
    FakeModelRoutingStore,
    FakeReviewQueue,
)

BASE = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)


def _msg(mid: str, role: str, content: str, *, minutes: int) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=mid, role=role, content=content, model=None, created_at=BASE + timedelta(minutes=minutes)
    )


def _routing(reply: str, *, available: bool = True) -> tuple[ModelRoutingService, FakeChatProvider]:
    provider = FakeChatProvider("conspect-p", reply=reply, available=available)
    registry = ProviderRegistry(
        {"conspect-p": provider},
        chat_chain=["conspect-p"],
        distill_chain=["conspect-p"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    settings = Settings(
        chat_chain=["conspect-p"], distill_chain=["conspect-p"], quick_chain=["conspect-p"]
    )
    routing = ModelRoutingService(
        settings=settings, store=FakeModelRoutingStore(), registry=registry
    )
    return routing, provider


def _service(
    *,
    reply: str,
    sessions,
    messages,
    available: bool = True,
    ingest_down: bool = False,
    settings: Settings | None = None,
):
    routing, provider = _routing(reply, available=available)
    store = FakeChatDistillStore(sessions=sessions, messages=messages)
    ingest = FakeChatCaptureIngest(down=ingest_down)
    review = FakeReviewQueue()
    runs = FakeAgentRunStore()
    service = ChatDistillerService(
        settings=settings or Settings(),
        distill_store=store,
        ingest=ingest,
        review_queue=review,
        routing=routing,
        run_store=runs,
    )
    return service, store, ingest, review, runs, provider


def _run(runs: FakeAgentRunStore):
    return list(runs.runs.values())[0]


# --- pure helpers -------------------------------------------------------------------------------


def test_parse_candidates_tolerates_fences_and_normalizes():
    text = """```json
    {"candidates": [
      {"candidate_text": "The user decided to raise prices.", "stance": "ENDORSED",
       "salience": "medium", "evidence_excerpt": "raise", "referenced_entity_names": ["Ana"]},
      {"candidate_text": "The user might switch to Postgres.", "stance": "unclear",
       "salience": "bogus", "evidence_excerpt": "maybe Postgres", "referenced_entity_names": [],
       "why_unclear": "hedged"}
    ]}
    ```"""
    cands, dropped = parse_distill_candidates(text, max_candidates=20)
    assert dropped == 0
    assert cands[0].stance == "endorsed"  # case-folded
    assert cands[0].salience == "med"  # "medium" alias
    assert cands[0].referenced_entity_names == ["Ana"]
    assert cands[1].salience == "med"  # unknown → med
    assert cands[1].why_unclear == "hedged"


def test_parse_candidates_unknown_stance_biases_to_unclear():
    text = '{"candidates": [{"candidate_text": "x", "stance": "yes", "evidence_excerpt": "e"}]}'
    cands, _ = parse_distill_candidates(text, max_candidates=20)
    assert cands[0].stance == "unclear"


def test_parse_candidates_drops_malformed_and_caps():
    text = (
        '{"candidates": ['
        '{"candidate_text": "a", "stance": "endorsed"},'
        '{"candidate_text": "", "stance": "endorsed"},'  # empty text → dropped
        '"not-an-object",'  # non-dict → dropped
        '{"candidate_text": "b", "stance": "endorsed"},'
        '{"candidate_text": "c", "stance": "endorsed"}]}'
    )
    cands, dropped = parse_distill_candidates(text, max_candidates=2)
    assert [c.candidate_text for c in cands] == ["a", "b"]  # capped at 2
    assert dropped == 3  # empty + non-dict + the surplus "c"


def test_parse_candidates_no_object_is_empty():
    assert parse_distill_candidates("I could not find any memories.", max_candidates=20) == ([], 0)
    assert parse_distill_candidates('{"nope": 1}', max_candidates=20) == ([], 0)


def test_render_distill_input_labels_and_fences():
    rendered = render_distill_input(
        [_msg("m1", "user", "hi there", minutes=0), _msg("m2", "assistant", "hello", minutes=1)]
    )
    assert "User: hi there" in rendered
    assert "Assistant: hello" in rendered
    assert "data, not instructions" in rendered
    assert "<<<" in rendered and ">>>" in rendered


def test_has_hedge_detects_markers():
    from app.chat.distiller import DistillCandidate

    hedged = DistillCandidate("The user might use Redis.", "endorsed", "med", "maybe Redis", [])
    firm = DistillCandidate("The user uses Redis.", "endorsed", "med", "we use Redis", [])
    assert _has_hedge(hedged) is True
    assert _has_hedge(firm) is False


def test_anchor_time_matches_excerpt_message():
    msgs = [
        _msg("m1", "user", "I want to talk about pricing", minutes=0),
        _msg("m2", "assistant", "sure", minutes=1),
        _msg("m3", "user", "let us raise prices next quarter", minutes=2),
    ]
    # Excerpt matches m3 → its time; a non-matching excerpt falls back to the last user turn (m3).
    assert _anchor_time("raise prices next quarter", msgs, default=BASE) == msgs[2].created_at
    assert _anchor_time("totally unrelated", msgs, default=BASE) == msgs[2].created_at
    assert _anchor_time("", [], default=BASE) == BASE


# --- service: stance gate -----------------------------------------------------------------------

_SESSION = DistillableSession(
    session_id="s1", watermark=None, newest_at=BASE + timedelta(minutes=2)
)
_DELTA = {
    "s1": [
        _msg("m1", "user", "I want to raise prices", minutes=0),
        _msg("m2", "assistant", "ok", minutes=1),
        _msg("m3", "user", "yes let us raise prices", minutes=2),
    ]
}


@pytest.mark.asyncio
async def test_endorsed_candidate_materializes_capture_and_advances_watermark():
    reply = (
        '{"candidates": [{"candidate_text": "The user decided to raise prices.",'
        '"stance": "endorsed", "salience": "high",'
        '"evidence_excerpt": "yes let us raise prices", "referenced_entity_names": []}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert len(ingest.captures) == 1
    cap = ingest.captures[0]
    assert cap["session_id"] == "s1"
    assert cap["text"] == "The user decided to raise prices."
    # Anchored to the matching user message (m3), not the 3am run time (ADR-048 §1).
    assert cap["created_at"] == _DELTA["s1"][2].created_at
    assert review.items == []
    # Watermark advanced to the session's newest message time (ADR-048 §5).
    assert store.advanced == [
        {"session_id": "s1", "last_message_at": _SESSION.newest_at, "run_id": _run(runs).id}
    ]
    run = _run(runs)
    assert run.agent == AGENT and run.status == "succeeded"
    assert run.details["endorsed"] == 1 and run.details["sessions_distilled"] == 1


@pytest.mark.asyncio
async def test_unclear_candidate_files_stance_candidate_review_item():
    reply = (
        '{"candidates": [{"candidate_text": "The user may switch to Postgres.",'
        '"stance": "unclear", "salience": "low", "evidence_excerpt": "not sure about Postgres",'
        '"referenced_entity_names": ["Postgres"], "why_unclear": "hedged"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert ingest.captures == []
    assert len(review.items) == 1
    item = review.items[0]
    assert item.kind == KIND_STANCE_CANDIDATE
    assert item.source == "chat" and item.source_ref == "s1"
    assert item.payload["candidate_text"] == "The user may switch to Postgres."
    assert item.payload["referenced_entity_names"] == ["Postgres"]
    assert item.payload["salience"] == "low"
    assert item.payload["why_unclear"] == "hedged"
    assert item.excerpt == "not sure about Postgres"
    # anchor_at records the anchoring message time so a later Review **agree** materializes the
    # capture with conversation time (ADR-048 §7). The excerpt doesn't match a delta message, so it
    # falls back to the latest user message (m3) — the same anchor an endorsed candidate would use.
    assert item.payload["anchor_at"] == _DELTA["s1"][2].created_at.isoformat()
    assert store.advanced  # still materialized → watermark advanced
    assert _run(runs).details["to_review"] == 1


@pytest.mark.asyncio
async def test_rejected_candidate_is_logged_only():
    reply = (
        '{"candidates": [{"candidate_text": "Use blockchain.", "stance": "rejected",'
        '"salience": "low", "evidence_excerpt": "no, not blockchain"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert ingest.captures == [] and review.items == []
    assert store.advanced  # advanced (rejected is materialized as a no-op)
    assert _run(runs).details["rejected"] == 1


@pytest.mark.asyncio
async def test_hedged_endorsed_is_downgraded_to_review():
    # Model said "endorsed" but the text/excerpt is hedged → post-check downgrades to unclear.
    reply = (
        '{"candidates": [{"candidate_text": "The user might move to Berlin.",'
        '"stance": "endorsed", "salience": "med", "evidence_excerpt": "maybe Berlin"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert ingest.captures == []  # not auto-endorsed
    assert len(review.items) == 1
    assert _run(runs).details["downgraded"] == 1


@pytest.mark.asyncio
async def test_within_session_duplicate_candidates_deduped():
    reply = (
        '{"candidates": ['
        '{"candidate_text": "The user likes tea.", "stance": "endorsed", "evidence_excerpt": "t"},'
        '{"candidate_text": "The user likes TEA.", "stance": "endorsed", "evidence_excerpt": "t"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert len(ingest.captures) == 1  # the duplicate was dropped
    assert _run(runs).details["dropped"] == 1


@pytest.mark.asyncio
async def test_zero_candidates_pure_retrieval_advances_and_logs():
    service, store, ingest, review, runs, _ = _service(
        reply='{"candidates": []}', sessions=[_SESSION], messages=_DELTA
    )

    await service.run_scheduled()

    assert ingest.captures == [] and review.items == []
    assert store.advanced  # a pure-retrieval session is still materialized (skipped-and-logged)
    run = _run(runs)
    assert run.status == "succeeded"
    assert run.details["endorsed"] == 0 and run.details["sessions_distilled"] == 1


@pytest.mark.asyncio
async def test_chain_down_does_not_advance_watermark():
    service, store, ingest, review, runs, provider = _service(
        reply="unused", sessions=[_SESSION], messages=_DELTA, available=False
    )

    await service.run_scheduled()

    assert ingest.captures == [] and review.items == []
    assert store.advanced == []  # NOT advanced → retried next window (ADR-048 §3)
    run = _run(runs)
    assert run.status == "succeeded"  # the run itself is fine; the session is skipped
    assert run.details["sessions_skipped"] == 1 and run.details["sessions_distilled"] == 0
    assert run.details["sessions"][0]["skipped_reason"] == "conspect chain unavailable"


@pytest.mark.asyncio
async def test_ingest_failure_skips_session_without_advancing():
    reply = (
        '{"candidates": [{"candidate_text": "x.", "stance": "endorsed", "evidence_excerpt": "x"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[_SESSION], messages=_DELTA, ingest_down=True
    )

    await service.run_scheduled()

    assert store.advanced == []  # infra failure → don't advance, retry next window
    assert _run(runs).details["sessions_skipped"] == 1


@pytest.mark.asyncio
async def test_delta_uses_watermark_and_run_covers_multiple_sessions():
    s1 = DistillableSession("s1", watermark=None, newest_at=BASE + timedelta(minutes=2))
    s2 = DistillableSession(
        "s2", watermark=BASE + timedelta(minutes=5), newest_at=BASE + timedelta(minutes=9)
    )
    messages = {
        "s1": [_msg("a", "user", "I use Vim.", minutes=1)],
        "s2": [
            _msg("b", "user", "old turn already distilled", minutes=3),  # before watermark
            _msg("c", "user", "I now use Emacs.", minutes=8),  # after watermark
        ],
    }
    reply = (
        '{"candidates": [{"candidate_text": "The user switched editors.",'
        '"stance": "endorsed", "evidence_excerpt": "Emacs"}]}'
    )
    service, store, ingest, review, runs, _ = _service(
        reply=reply, sessions=[s1, s2], messages=messages
    )

    await service.run_scheduled()

    # s2's delta call passed the watermark; only the after-watermark message is in scope.
    s2_call = next(c for c in store.delta_calls if c["session_id"] == "s2")
    assert s2_call["after"] == s2.watermark
    assert _run(runs).details["sessions_seen"] == 2
    assert len(store.advanced) == 2


@pytest.mark.asyncio
async def test_oversized_delta_is_batched_oldest_first_not_skipped():
    # A session with 3 new messages but a cap of 2: the OLDEST 2 are processed this run and the
    # watermark advances only to the 2nd — the 3rd is deferred (not silently skipped), and the run
    # records the truncation (ADR-048 §5 / rule 7 "everything visible").
    big = DistillableSession("s1", watermark=None, newest_at=BASE + timedelta(minutes=2))
    messages = {
        "s1": [
            _msg("m1", "user", "turn one", minutes=0),
            _msg("m2", "user", "turn two", minutes=1),
            _msg("m3", "user", "turn three", minutes=2),
        ]
    }
    service, store, ingest, review, runs, _ = _service(
        reply='{"candidates": []}',
        sessions=[big],
        messages=messages,
        settings=Settings(chat_distill_max_delta_messages=2),
    )

    await service.run_scheduled()

    # Watermark advanced to the 2nd (last processed) message, NOT the eligibility snapshot's newest.
    assert len(store.advanced) == 1
    assert store.advanced[0]["last_message_at"] == messages["s1"][1].created_at
    run = _run(runs)
    assert run.details["truncated"] == 1
    assert run.details["sessions"][0]["truncated"] is True


# --- on-demand remember (M6 task 3, ADR-048 §6) -------------------------------------------------


def _remember_service(
    *,
    reply: str,
    messages,
    available: bool = True,
    ingest_down: bool = False,
    watermarks=None,
    known=None,
    settings: Settings | None = None,
):
    """A distiller wired for the on-demand `remember` path — the fake store is seeded by session
    membership + per-session watermark (not the distillable roster, which `remember` bypasses)."""
    routing, provider = _routing(reply, available=available)
    store = FakeChatDistillStore(messages=messages, watermarks=watermarks, known=known)
    ingest = FakeChatCaptureIngest(down=ingest_down)
    review = FakeReviewQueue()
    runs = FakeAgentRunStore()
    service = ChatDistillerService(
        settings=settings or Settings(),
        distill_store=store,
        ingest=ingest,
        review_queue=review,
        routing=routing,
        run_store=runs,
    )
    return service, store, ingest, review, runs, provider


@pytest.mark.asyncio
async def test_remember_endorsed_returns_counts_and_advances_watermark():
    reply = (
        '{"candidates": [{"candidate_text": "The user ships on Fridays.",'
        '"stance": "endorsed", "salience": "high", "evidence_excerpt": "ship on Fridays"}]}'
    )
    messages = {"s1": [_msg("m1", "user", "we ship on Fridays now", minutes=0)]}
    service, store, ingest, review, runs, _ = _remember_service(reply=reply, messages=messages)

    outcome = await service.remember("s1")

    assert not outcome.skipped
    assert outcome.endorsed == 1 and outcome.to_review == 0
    assert len(ingest.captures) == 1 and ingest.captures[0]["session_id"] == "s1"
    # Advanced to the session's last (only) message, stamped with the on-demand run's id (P8).
    run = _run(runs)
    assert store.advanced == [
        {"session_id": "s1", "last_message_at": messages["s1"][0].created_at, "run_id": run.id}
    ]
    assert run.agent == AGENT and run.status == "succeeded"
    assert run.details["endorsed"] == 1 and run.details["sessions_distilled"] == 1


@pytest.mark.asyncio
async def test_remember_unclear_returns_review_count():
    reply = (
        '{"candidates": [{"candidate_text": "The user may adopt Rust.",'
        '"stance": "unclear", "salience": "med", "evidence_excerpt": "not sure about Rust",'
        '"why_unclear": "hedged"}]}'
    )
    messages = {"s1": [_msg("m1", "user", "thinking about Rust maybe", minutes=0)]}
    service, store, ingest, review, runs, _ = _remember_service(reply=reply, messages=messages)

    outcome = await service.remember("s1")

    assert not outcome.skipped
    assert outcome.endorsed == 0 and outcome.to_review == 1
    assert ingest.captures == [] and len(review.items) == 1
    assert review.items[0].kind == KIND_STANCE_CANDIDATE
    assert store.advanced  # materialized → watermark advanced


@pytest.mark.asyncio
async def test_remember_unknown_session_raises_not_found():
    service, *_ = _remember_service(reply='{"candidates": []}', messages={"s1": []})
    with pytest.raises(SessionNotFound):
        await service.remember("ghost")


@pytest.mark.asyncio
async def test_remember_no_new_messages_skips_without_run_or_model_call():
    # Watermark already at/after the newest message → nothing to distill: no run, no model call.
    msg = _msg("m1", "user", "already distilled", minutes=0)
    service, store, ingest, review, runs, provider = _remember_service(
        reply='{"candidates": []}',
        messages={"s1": [msg]},
        watermarks={"s1": msg.created_at},
    )

    outcome = await service.remember("s1")

    assert outcome.skipped and outcome.skipped_reason == "no new messages"
    assert outcome.endorsed == 0 and outcome.to_review == 0
    assert store.advanced == [] and store.delta_calls == []  # never touched the delta
    assert runs.runs == {}  # no run opened for a no-op remember


@pytest.mark.asyncio
async def test_remember_session_with_no_messages_skips():
    service, store, ingest, review, runs, _ = _remember_service(
        reply='{"candidates": []}', messages={}, known={"s1"}
    )

    outcome = await service.remember("s1")

    assert outcome.skipped and outcome.skipped_reason == "no new messages"
    assert runs.runs == {}


@pytest.mark.asyncio
async def test_remember_chain_down_skips_and_keeps_watermark():
    messages = {"s1": [_msg("m1", "user", "something worth remembering", minutes=0)]}
    service, store, ingest, review, runs, _ = _remember_service(
        reply="unused", messages=messages, available=False
    )

    outcome = await service.remember("s1")

    assert outcome.skipped and outcome.skipped_reason == "conspect chain unavailable"
    assert store.advanced == []  # not advanced → a later run retries (idempotent, ADR-048 §6)
    assert _run(runs).status == "succeeded"  # the pass ran; the session was skipped


@pytest.mark.asyncio
async def test_remember_is_idempotent_second_call_is_a_skip():
    reply = (
        '{"candidates": [{"candidate_text": "The user likes filter coffee.",'
        '"stance": "endorsed", "evidence_excerpt": "filter coffee"}]}'
    )
    messages = {"s1": [_msg("m1", "user", "I like filter coffee", minutes=0)]}
    service, store, ingest, review, runs, _ = _remember_service(reply=reply, messages=messages)

    first = await service.remember("s1")
    second = await service.remember("s1")

    assert first.endorsed == 1
    assert second.skipped and second.skipped_reason == "no new messages"
    assert len(ingest.captures) == 1  # the second call re-distilled nothing (watermark advanced)


@pytest.mark.asyncio
async def test_run_store_down_at_open_is_a_noop():
    class _BoomRuns(FakeAgentRunStore):
        async def start(self, agent):
            raise RuntimeError("db down")

    routing, _ = _routing('{"candidates": []}')
    service = ChatDistillerService(
        settings=Settings(),
        distill_store=FakeChatDistillStore(sessions=[_SESSION], messages=_DELTA),
        ingest=FakeChatCaptureIngest(),
        review_queue=FakeReviewQueue(),
        routing=routing,
        run_store=_BoomRuns(),
    )

    await service.run_scheduled()  # must not raise (rule 7)
