"""Entity browse/search unit tests (M9.8 T2 / ADR-064 §2 — the merge picker's read).

`rank_entity_matches` is pure name-matching logic (08 testing policy → unit-tested, no DB); the
`EntityBrowseService` layer is exercised against the in-memory `FakeEntityStore`.
"""

from __future__ import annotations

from app.entities.entity_browse import EntityBrowseService, rank_entity_matches
from app.entities.entity_store import EntityRef

from .fakes import FakeEntityStore


def _ref(id: str, title: str | None, *, type: str = "person", aliases=None) -> EntityRef:
    return EntityRef(
        id=id, type=type, title=title, aliases=list(aliases or []), store_path=f"person/{id}.md"
    )


# --- rank_entity_matches (pure) -----------------------------------------------------------------


def test_empty_query_is_alphabetical_browse():
    refs = [_ref("c", "Diana Wren"), _ref("a", "Alex"), _ref("b", "diana vance")]
    out = rank_entity_matches(refs, None, limit=10)
    assert [r.title for r in out] == ["Alex", "diana vance", "Diana Wren"]


def test_query_filters_to_name_or_alias_substring():
    refs = [
        _ref("1", "Diana Vance"),
        _ref("2", "Diana Wren"),
        _ref("3", "Alex", aliases=["Diana's brother"]),  # alias contains "diana"
        _ref("4", "Bob"),
    ]
    out = rank_entity_matches(refs, "diana", limit=10)
    assert {r.id for r in out} == {"1", "2", "3"}  # Bob excluded


def test_ranking_prefers_exact_then_prefix_then_alias_then_contains():
    refs = [
        _ref("contains", "Adiana Smith"),  # contains, not prefix
        _ref("prefix", "Diana Vance"),  # title prefix
        _ref("exact", "Diana"),  # exact title
        _ref("alias", "Someone", aliases=["Diana"]),  # exact alias
    ]
    out = rank_entity_matches(refs, "Diana", limit=10)
    assert [r.id for r in out] == ["exact", "prefix", "alias", "contains"]


def test_matching_is_diacritic_folded_and_case_insensitive():
    # "Madalina" must find a hub stored as "Mădălina" (ADR-041 folding parity), case-insensitively.
    refs = [_ref("1", "Mădălina Fairfax"), _ref("2", "Horia")]
    out = rank_entity_matches(refs, "madalina", limit=10)
    assert [r.id for r in out] == ["1"]


def test_limit_truncates_after_ranking():
    refs = [_ref(str(i), f"Diana {i}") for i in range(5)]
    out = rank_entity_matches(refs, "diana", limit=2)
    assert len(out) == 2


def test_non_positive_limit_returns_empty():
    assert rank_entity_matches([_ref("1", "Diana")], "diana", limit=0) == []


def test_untitled_hub_sorts_last_but_still_alias_matchable():
    refs = [_ref("named", "Diana"), _ref("untitled", None, aliases=["Diana V"])]
    out = rank_entity_matches(refs, None, limit=10)
    assert [r.id for r in out] == ["named", "untitled"]  # untitled last in browse


# --- EntityBrowseService (over the fake store) --------------------------------------------------


async def test_service_defaults_to_all_entity_like_types_when_type_omitted():
    store = FakeEntityStore(
        entities=[
            _ref("p", "Diana", type="person"),
            _ref("t", "Diana topic", type="topic"),
            _ref("m", "Diana memory", type="memory"),  # not entity-like → never listed
        ]
    )
    svc = EntityBrowseService(store=store, entity_like_types=["person", "topic", "event"])
    out = await svc.browse(type_filter=None, q="diana", limit=10)
    assert {r.id for r in out} == {"p", "t"}


async def test_service_narrows_to_one_type_when_given():
    store = FakeEntityStore(
        entities=[_ref("p", "Diana", type="person"), _ref("t", "Diana topic", type="topic")]
    )
    svc = EntityBrowseService(store=store, entity_like_types=["person", "topic"])
    out = await svc.browse(type_filter="person", q=None, limit=10)
    assert [r.id for r in out] == ["p"]
