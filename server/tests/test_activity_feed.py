"""Unit tests for the merged activity-feed service (03-api §Activity, ADR-053 §4/§5).

Exercises the category resolution, keyset cursor encode/decode, and limit clamp over an in-memory
:class:`FakeActivityFeedStore` that mirrors ``PgActivityFeedStore.read`` (category filter + keyset
on ``(ts, id)`` + ``ts DESC, id DESC`` order + limit) — no live DB (08 testing policy).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.activity_feed import (
    CATEGORY_AGENTS_JOBS,
    CATEGORY_CONVERSATIONS,
    CATEGORY_MANUAL_ACTIONS,
    FEED_MAX_LIMIT,
    ActivityFeedService,
    ActivityRow,
    InvalidActivityCursor,
    _decode_cursor,
    _encode_cursor,
)

BASE = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


def _row(
    id: str,
    category: str,
    *,
    offset_seconds: int = 0,
    kind: str = "agent_run",
    parent_ref: str | None = None,
) -> ActivityRow:
    return ActivityRow(
        id=id,
        category=category,
        kind=kind,
        ts=BASE + timedelta(seconds=offset_seconds),
        title=f"title-{id}",
        snippet=f"snippet-{id}",
        ref=id,
        parent_ref=parent_ref,
    )


class FakeActivityFeedStore:
    """In-memory feed store mirroring the real projection: filter by requested categories, keyset
    strictly before ``(ts, id)``, order ``ts DESC, id DESC``, cap at ``limit``."""

    def __init__(self, rows: list[ActivityRow]) -> None:
        self._rows = list(rows)
        self.calls: list[dict] = []

    async def read(self, *, categories, before, limit):
        self.calls.append({"categories": set(categories), "before": before, "limit": limit})
        rows = [r for r in self._rows if r.category in categories]
        rows.sort(key=lambda r: (r.ts, r.id), reverse=True)
        if before is not None:
            rows = [r for r in rows if (r.ts, r.id) < before]
        return rows[:limit]


def _service(rows: list[ActivityRow]) -> tuple[ActivityFeedService, FakeActivityFeedStore]:
    store = FakeActivityFeedStore(rows)
    return ActivityFeedService(store), store


async def test_no_category_reads_all_three():
    rows = [
        _row("a", CATEGORY_AGENTS_JOBS, offset_seconds=1),
        _row("c", CATEGORY_CONVERSATIONS, offset_seconds=2),
        _row("m", CATEGORY_MANUAL_ACTIONS, offset_seconds=3),
    ]
    service, store = _service(rows)
    page = await service.feed()
    assert {i.category for i in page.items} == {
        CATEGORY_AGENTS_JOBS,
        CATEGORY_CONVERSATIONS,
        CATEGORY_MANUAL_ACTIONS,
    }
    # All three categories reach the store when the filter is omitted.
    assert store.calls[0]["categories"] == {
        CATEGORY_AGENTS_JOBS,
        CATEGORY_CONVERSATIONS,
        CATEGORY_MANUAL_ACTIONS,
    }


async def test_category_filter_narrows_to_one_tab():
    rows = [
        _row("a", CATEGORY_AGENTS_JOBS, offset_seconds=1),
        _row("c", CATEGORY_CONVERSATIONS, offset_seconds=2),
        _row("m", CATEGORY_MANUAL_ACTIONS, offset_seconds=3),
    ]
    service, store = _service(rows)
    page = await service.feed(category=CATEGORY_CONVERSATIONS)
    assert [i.id for i in page.items] == ["c"]
    assert store.calls[0]["categories"] == {CATEGORY_CONVERSATIONS}


async def test_newest_first_order_with_id_tiebreak():
    # Two rows share a ts → the id is the deterministic (descending) tiebreaker.
    rows = [
        _row("aaa", CATEGORY_AGENTS_JOBS, offset_seconds=5),
        _row("bbb", CATEGORY_AGENTS_JOBS, offset_seconds=5),
        _row("ccc", CATEGORY_AGENTS_JOBS, offset_seconds=9),
    ]
    service, _ = _service(rows)
    page = await service.feed()
    assert [i.id for i in page.items] == ["ccc", "bbb", "aaa"]


async def test_keyset_pagination_walks_without_overlap():
    rows = [_row(f"r{n}", CATEGORY_AGENTS_JOBS, offset_seconds=n) for n in range(5)]
    service, _ = _service(rows)

    first = await service.feed(limit=2)
    assert [i.id for i in first.items] == ["r4", "r3"]
    assert first.next_before is not None

    second = await service.feed(limit=2, before=first.next_before)
    assert [i.id for i in second.items] == ["r2", "r1"]
    assert second.next_before is not None

    third = await service.feed(limit=2, before=second.next_before)
    assert [i.id for i in third.items] == ["r0"]
    # The last (short) page exhausts the feed — no dangling cursor.
    assert third.next_before is None


async def test_next_before_none_when_page_exactly_drains_feed():
    rows = [_row(f"r{n}", CATEGORY_AGENTS_JOBS, offset_seconds=n) for n in range(2)]
    service, _ = _service(rows)
    page = await service.feed(limit=2)
    assert [i.id for i in page.items] == ["r1", "r0"]
    # Read `limit + 1` found no further row → no next cursor even though the page filled.
    assert page.next_before is None


async def test_limit_clamped_to_max():
    service, store = _service([])
    await service.feed(limit=10_000)
    # Store is asked for `max + 1` (the has-more probe) — never the unbounded request.
    assert store.calls[0]["limit"] == FEED_MAX_LIMIT + 1


async def test_malformed_cursor_raises():
    service, _ = _service([])
    with pytest.raises(InvalidActivityCursor):
        await service.feed(before="not-a-real-cursor")


def test_cursor_round_trips():
    row = _row("abc", CATEGORY_AGENTS_JOBS, offset_seconds=7)
    ts, rid = _decode_cursor(_encode_cursor(row))
    assert rid == "abc"
    assert ts == row.ts


def test_decode_rejects_wrong_shape():
    import base64
    import json

    # A well-formed base64/JSON blob but the wrong arity is still an invalid cursor.
    blob = base64.urlsafe_b64encode(json.dumps(["only-one"]).encode()).decode()
    with pytest.raises(InvalidActivityCursor):
        _decode_cursor(blob)
