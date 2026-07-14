"""EntityResolver tests — token-overlap retrieval + alias accretion + the exact/fuzzy gate
(ADR-040, M3 task 11). Fakes only: no live DB/LLM (08 testing policy)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.entities.resolver import EntityResolver, Mention, significant_tokens
from app.entities.store import EntityCandidate
from app.providers.registry import ProviderRegistry

from .fakes import FakeAliasStore, FakeChatProvider, FakeReviewQueue

CREATED = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _resolver(alias_store: FakeAliasStore, *, reply: str = '{"choice": "none", "conf": 0.0}'):
    settings = Settings(scheduler_tz="UTC")
    chat = FakeChatProvider("fake-chat", reply=reply)
    registry = ProviderRegistry(
        {"fake-chat": chat},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    review = FakeReviewQueue()
    resolver = EntityResolver(
        settings=settings, alias_store=alias_store, review_queue=review, registry=registry
    )
    return resolver, review, chat


async def _resolve(resolver, name: str, *, rel: str = "involves", etype: str = "person"):
    return await resolver.resolve(
        [Mention(name=name, type=etype, rel=rel)],
        source="text",
        source_ref="cap-1",
        created_local=CREATED,
        since="2026-07-14",
        excerpt="context",
    )


# --- significant_tokens (the low-entropy guard) -----------------------------------------


def test_significant_tokens_filters_short_and_stop_tokens():
    stop = {"the", "mom"}
    assert significant_tokens("Horia Fenwick", min_len=4, stop=stop) == ["horia", "fenwick"]
    assert significant_tokens("Ana", min_len=4, stop=stop) == []  # too short
    assert significant_tokens("the mom", min_len=2, stop=stop) == []  # stop tokens
    # Folded + lower-cased (ADR-041) so it matches stored forms.
    assert significant_tokens("Mădălina", min_len=4, stop=set()) == ["madalina"]


# --- exact short-circuit (no LLM) -------------------------------------------------------


async def test_exact_hit_auto_links_without_llm():
    alias = FakeAliasStore(
        candidates_by_key={
            ("horia", "person"): [
                EntityCandidate(id="horia-1", type="person", title="Horia",
                                aliases=["Horia"], store_path="person/horia--h1.md")
            ]
        }
    )
    resolver, _, chat = _resolver(alias)
    result = await _resolve(resolver, "Horia")
    assert result.links[("horia", "person")].entity_id == "horia-1"
    assert chat.calls == 0  # exact short-circuit — never a round-trip
    assert result.accretions == []  # surface already an alias


# --- token-overlap retrieval + accretion (the Horia / Horia Fenwick fix) ----------------


async def test_variant_surfaces_existing_hub_then_llm_confirms_and_accretes():
    hub = EntityCandidate(
        id="horia-1", type="person", title="Horia", aliases=["Horia"],
        store_path="person/horia--h1.md",
    )
    # No exact key for "horia fenwick"; the hub is only reachable via the token-overlap pool.
    alias = FakeAliasStore(entities=[hub])
    resolver, review, chat = _resolver(alias, reply='{"choice": "horia-1", "conf": 0.95}')
    result = await _resolve(resolver, "Horia Fenwick")

    # Retrieval surfaced the hub (token overlap on "horia"); the LLM was consulted (fuzzy).
    assert alias.token_calls[-1] == ("horia", "fenwick")
    assert chat.calls == 1
    # Confident pick → link, and the new surface form is accreted onto the hub.
    assert result.links[("horia fenwick", "person")].entity_id == "horia-1"
    assert len(result.accretions) == 1
    acc = result.accretions[0]
    assert acc.entity_id == "horia-1"
    assert acc.surface == "Horia Fenwick"
    assert "Horia Fenwick" in acc.aliases and "Horia" in acc.aliases
    assert review.items == []  # confident → no review


async def test_fuzzy_single_candidate_low_confidence_goes_to_review_never_guessed():
    hub = EntityCandidate(
        id="horia-1", type="person", title="Horia", aliases=["Horia"],
        store_path="person/horia--h1.md",
    )
    alias = FakeAliasStore(entities=[hub])
    resolver, review, chat = _resolver(alias, reply='{"choice": "horia-1", "conf": 0.4}')
    result = await _resolve(resolver, "Horia Fenwick")

    assert chat.calls == 1
    assert ("horia fenwick", "person") not in result.links  # pending — never guessed
    assert result.pending == 1
    assert review.items and review.items[0].kind == "entity-ambiguity"
    assert result.accretions == []


async def test_low_entropy_mention_uses_exact_only_no_token_leg():
    alias = FakeAliasStore()
    resolver, _, chat = _resolver(alias)
    # "Ana" is below entity_min_token_len → no significant tokens → token leg off.
    await _resolve(resolver, "Ana")
    assert alias.token_calls[-1] == ()  # exact-only retrieval


async def test_no_candidates_mints_a_new_hub():
    alias = FakeAliasStore()
    resolver, _, _ = _resolver(alias)
    result = await _resolve(resolver, "Zorblax")
    assert len(result.new_documents) == 1
    assert result.new_documents[0].type == "person"
    assert result.links[("zorblax", "person")].entity_id == result.new_documents[0].id
    assert result.accretions == []


async def test_exact_hit_alongside_fuzzy_still_short_circuits():
    exact = EntityCandidate(id="horia-1", type="person", title="Horia", aliases=["Horia"],
                            store_path="person/horia--h1.md")
    fuzzy = EntityCandidate(id="horia-2", type="person", title="Horia Delgado",
                            aliases=["Horia Delgado"], store_path="person/horia-delgado--h2.md")
    alias = FakeAliasStore(
        candidates_by_key={("horia", "person"): [exact]}, entities=[exact, fuzzy]
    )
    resolver, _, chat = _resolver(alias)
    result = await _resolve(resolver, "Horia")
    # A single exact hit wins even though a fuzzy candidate also surfaced — no LLM round-trip.
    assert result.links[("horia", "person")].entity_id == "horia-1"
    assert chat.calls == 0
