"""Merged activity-feed projection (03-api §Activity, ADR-053 §4/§5).

The merged ``GET /activity`` feed is a **UNION-of-views projection**, not a new events table
(ADR-053 §4: a dedicated table would be a derived copy of ``agent_runs``/``captures``/
``review_queue`` that drifts from the source rows — against rule 1's durable-truth spirit; the
source rows *are* the events). Each source row is normalized to a common
``{id, category, kind, ts, title, snippet, ref}`` shape (+ ``parent_ref`` for the pipeline
parent→child nesting, ADR-053 §11; + ``status``/``source`` for the M8.1 Captures row, ADR-054 §4),
the union is ordered ``ts DESC`` and **keyset-paginated on ``(ts, id)``** via the opaque ``before=``
cursor.

**Category is by *origin*, not table** (ADR-053 §5): the same ``agent_runs`` row is
``agents_jobs`` when scheduled and ``manual_actions`` when hand-triggered, read off the M8
``agent_runs.trigger`` column — a hand-run ``reindex`` lands under manual actions rather than
looking like a nightly job. The three categories:

* **agents_jobs** — scheduled ``agent_runs``, **parentless runs only** (M8.1 ADR-054 §2: a pipeline
  run is one row; its step children live under ``GET /activity/runs/{id}``'s recursive tree).
* **captures** — all captures regardless of source (voice/text/mcp/chat), keyset-paginated (M8.1
  ADR-054 §4; renamed from ``conversations``, widened from chat-only). The M6 "recently
  auto-recorded" chat list folds in here; one-tap-removed captures excluded via ``removed_at``.
* **manual_actions** — human-initiated ops: manually-triggered ``agent_runs`` (also parentless
  only) + review verdicts (resolved ``review_queue`` items).

:class:`ActivityFeedService` owns the cursor encode/decode + limit clamp (business logic, rule 5);
:class:`PgActivityFeedStore` is the plain-SQL asyncpg read (no ORM, rule 5 / ADR-011). The service
depends on the :class:`ActivityFeedStore` protocol so it unit-tests against an in-memory fake (no
live DB in CI — 08 testing policy).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ..db import Database

# --- Categories (ADR-053 §4; M8.1 ADR-054 §4 renamed conversations→captures) --------------------
CATEGORY_AGENTS_JOBS = "agents_jobs"
# M8.1 (ADR-054 §4): the old ``conversations`` category becomes ``captures`` — it now carries *all*
# captures (voice/text/mcp/chat), not only auto-endorsed chat memories. The wire value changed with
# it (03-api §Activity M8.1 addendum); chat rows still fold their one-tap-remove loop in here.
CATEGORY_CAPTURES = "captures"
CATEGORY_MANUAL_ACTIONS = "manual_actions"

# The wire enum for the ``?category=`` filter; typing the query param with it makes FastAPI 422 an
# unknown value at the boundary (no service-side validation needed).
ActivityCategory = Literal["agents_jobs", "captures", "manual_actions"]
VALID_CATEGORIES: tuple[str, ...] = (
    CATEGORY_AGENTS_JOBS,
    CATEGORY_CAPTURES,
    CATEGORY_MANUAL_ACTIONS,
)

# Normalized row kinds (the source-entity discriminator the client renders on; the specific agent
# name / review kind rides in ``title``). M8.1 (ADR-054 §4): the capture kind is ``capture`` (was
# ``chat_capture``) now that the Captures branch carries every source, not only chat memories.
KIND_AGENT_RUN = "agent_run"
KIND_CAPTURE = "capture"
KIND_REVIEW_VERDICT = "review_verdict"

# Page-size + snippet bounds. Kept as module constants (not config knobs) because M8 Batch B owns no
# config edits; the coordinator can promote these beside `review_batch_max`/`run_log_tail_max_lines`
# later. `FEED_DEFAULT_LIMIT` matches the 03-api `limit=50` default; over-large requests clamp
# silently to `FEED_MAX_LIMIT` (the SearchService `top_k` clamp pattern), never 422.
FEED_DEFAULT_LIMIT = 50
FEED_MAX_LIMIT = 100
# Snippet chars kept per row (a `title`/`summary`/`excerpt`/`raw_text` preview — `raw_text` can be
# long, so the read truncates in SQL to avoid transferring the whole capture body).
SNIPPET_MAX_CHARS = 280

# A keyset position: the ``(ts, id)`` of the row the next page resumes strictly before.
FeedCursor = tuple[datetime, str]


class InvalidActivityCursor(Exception):
    """A ``before=`` cursor that doesn't decode — tampered, truncated, or from another shape. The
    router maps it to ``422`` (mirrors the map/neighbors ``InvalidCursor`` handling)."""


@dataclass(frozen=True)
class ActivityRow:
    """One normalized feed row (a projection of an ``agent_runs``/``captures``/``review_queue``
    source row). ``ref`` is the drill-down target (a run id → ``GET /activity/runs/{id}``, a chat
    session id → open the conversation, a review id); ``parent_ref`` links a pipeline step child to
    its parent run (``None`` otherwise).

    ``status`` + ``source`` (M8.1, ADR-054 §4) carry the source row's lifecycle status and — for a
    capture — its origin (``COALESCE(source, kind)`` → ``text``/``voice``/``mcp``/``chat``), so a
    Captures row renders its status + source badge without a per-row detail fetch. ``source`` is
    ``None`` for the non-capture branches."""

    id: str
    category: str
    kind: str
    ts: datetime
    title: str | None
    snippet: str | None
    ref: str | None
    parent_ref: str | None
    status: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ActivityFeedPage:
    """One keyset page: the rows + the opaque ``next_before`` cursor to pass back as ``before=``
    (``None`` at the end of the feed)."""

    items: list[ActivityRow]
    next_before: str | None


class ActivityFeedStore(Protocol):
    """The read surface the feed service composes over."""

    async def read(
        self, *, categories: set[str], before: FeedCursor | None, limit: int
    ) -> list[ActivityRow]:
        """Newest-first (``ts DESC, id DESC``) rows across the requested ``categories``, strictly
        keyset-before ``before`` when given, capped at ``limit``."""
        ...


class ActivityFeedService:
    """The merged-feed read: category resolution, keyset cursor encode/decode, limit clamp."""

    def __init__(
        self,
        store: ActivityFeedStore,
        *,
        default_limit: int = FEED_DEFAULT_LIMIT,
        max_limit: int = FEED_MAX_LIMIT,
    ) -> None:
        self._store = store
        self._default_limit = default_limit
        self._max_limit = max_limit

    async def feed(
        self,
        *,
        category: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> ActivityFeedPage:
        """One feed page. ``category`` (already validated to a known value by the router's Literal,
        or ``None`` for all three) selects which source branches contribute; ``before`` is the
        opaque cursor from the prior page (raises :class:`InvalidActivityCursor` if malformed).

        Reads ``limit + 1`` to detect a further page without a trailing empty request — returns the
        first ``limit`` rows and a ``next_before`` cursor only when a further row exists."""
        categories = {category} if category else set(VALID_CATEGORIES)
        cursor = _decode_cursor(before) if before is not None else None
        page_limit = self._clamp_limit(limit)
        rows = await self._store.read(categories=categories, before=cursor, limit=page_limit + 1)
        has_more = len(rows) > page_limit
        page = rows[:page_limit]
        next_before = _encode_cursor(page[-1]) if has_more and page else None
        return ActivityFeedPage(items=page, next_before=next_before)

    def _clamp_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._default_limit
        return max(1, min(limit, self._max_limit))


def _encode_cursor(row: ActivityRow) -> str:
    """Opaque, URL-safe token carrying the ``(ts, id)`` keyset the next page resumes strictly
    before. Base64 of compact JSON — clients treat it as a blob (mirrors the map cursor)."""
    raw = json.dumps([row.ts.isoformat(), row.id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> FeedCursor:
    """Reverse of :func:`_encode_cursor`. Raises :class:`InvalidActivityCursor` on anything that
    isn't a well-formed ``[iso_ts, id]`` keyset (tampered, truncated, or from an incompatible
    shape)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        parts = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise InvalidActivityCursor(cursor) from exc
    if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(p, str) for p in parts):
        raise InvalidActivityCursor(cursor)
    try:
        ts = datetime.fromisoformat(parts[0])
    except ValueError as exc:
        raise InvalidActivityCursor(cursor) from exc
    return (ts, parts[1])


def _row(record: object) -> ActivityRow:
    # asyncpg Record → ActivityRow. The SELECT column list is fixed by the union below.
    return ActivityRow(
        id=record["id"],  # type: ignore[index]
        category=record["category"],  # type: ignore[index]
        kind=record["kind"],  # type: ignore[index]
        ts=record["ts"],  # type: ignore[index]
        title=record["title"],  # type: ignore[index]
        snippet=record["snippet"],  # type: ignore[index]
        ref=record["ref"],  # type: ignore[index]
        parent_ref=record["parent_ref"],  # type: ignore[index]
        status=record["status"],  # type: ignore[index]
        source=record["source"],  # type: ignore[index]
    )


# The per-source SELECT branches of the UNION. Every category/kind/trigger literal here is our own
# constant (never user input), so inlining them is injection-safe; the only user-supplied values —
# the keyset cursor + limit — are bound parameters in :meth:`PgActivityFeedStore.read`. Each branch
# emits the same 10 columns in the same order/types so the ``UNION ALL`` type-checks.
#
# M8.1 (ADR-054 §2): the agent_runs branch returns **only parentless runs** (``parent_run_id IS
# NULL``) — a pipeline run is one feed row (its rollup summary already carries the step counts); the
# step children live under ``GET /activity/runs/{id}``'s recursive ``children[]`` tree.
# ``parent_ref`` stays on the wire for compatibility but is always NULL here now (parentless rows).
_AGENT_RUNS_BRANCH = f"""
    SELECT
        r.id::text AS id,
        CASE WHEN r.trigger = 'manual' THEN '{CATEGORY_MANUAL_ACTIONS}'
             ELSE '{CATEGORY_AGENTS_JOBS}' END AS category,
        '{KIND_AGENT_RUN}' AS kind,
        r.started_at AS ts,
        r.agent AS title,
        left(r.summary, {SNIPPET_MAX_CHARS}) AS snippet,
        r.id::text AS ref,
        r.parent_run_id::text AS parent_ref,
        r.status AS status,
        NULL::text AS source
    FROM agent_runs r
    WHERE r.parent_run_id IS NULL{{agent_runs_and}}
"""

# M8.1 (ADR-054 §4): all captures regardless of source (was ``source = 'chat'`` only). ``source`` is
# the origin badge ``COALESCE(source, kind)`` (a web text/voice capture has NULL ``source`` → falls
# back to its kind); ``status`` is the capture lifecycle. One-tap-removed captures stay excluded via
# ``removed_at``. ``ref`` keeps the chat-session id (``source_ref``) so chat rows still open their
# conversation; the row id is the capture id the client expands via ``GET /captures/{id}``.
_CAPTURES_BRANCH = f"""
    SELECT
        c.id::text AS id,
        '{CATEGORY_CAPTURES}' AS category,
        '{KIND_CAPTURE}' AS kind,
        c.created_at AS ts,
        NULL::text AS title,
        left(c.raw_text, {SNIPPET_MAX_CHARS}) AS snippet,
        c.source_ref AS ref,
        NULL::text AS parent_ref,
        c.status AS status,
        COALESCE(c.source, c.kind) AS source
    FROM captures c
    WHERE c.removed_at IS NULL
"""

_REVIEW_BRANCH = f"""
    SELECT
        q.id::text AS id,
        '{CATEGORY_MANUAL_ACTIONS}' AS category,
        '{KIND_REVIEW_VERDICT}' AS kind,
        q.resolved_at AS ts,
        q.kind AS title,
        left(q.excerpt, {SNIPPET_MAX_CHARS}) AS snippet,
        q.id::text AS ref,
        NULL::text AS parent_ref,
        q.status AS status,
        NULL::text AS source
    FROM review_queue q
    WHERE q.resolved_at IS NOT NULL
"""


class PgActivityFeedStore:
    """asyncpg-backed feed projection — plain SQL, no ORM (ADR-011). A single ``UNION ALL`` over the
    requested source branches, keyset-filtered + ordered + limited in the outer query."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _branches(self, categories: set[str]) -> list[str]:
        branches: list[str] = []
        # `agent_runs` feeds both job categories; the trigger filter narrows the branch when only
        # one is requested (both requested → no filter, the CASE assigns each row by origin).
        want_scheduled = CATEGORY_AGENTS_JOBS in categories
        want_manual = CATEGORY_MANUAL_ACTIONS in categories
        # The branch always filters `parent_run_id IS NULL` (M8.1 §2); the trigger predicate is
        # ANDed on when only one of the two run categories is requested (both → the CASE assigns
        # each row by origin, no trigger filter needed).
        if want_scheduled and want_manual:
            branches.append(_AGENT_RUNS_BRANCH.format(agent_runs_and=""))
        elif want_scheduled:
            branches.append(
                _AGENT_RUNS_BRANCH.format(agent_runs_and=" AND r.trigger = 'scheduled'")
            )
        elif want_manual:
            branches.append(_AGENT_RUNS_BRANCH.format(agent_runs_and=" AND r.trigger = 'manual'"))
        if CATEGORY_CAPTURES in categories:
            branches.append(_CAPTURES_BRANCH)
        if want_manual:  # review verdicts are human-initiated → manual actions
            branches.append(_REVIEW_BRANCH)
        return branches

    async def read(
        self, *, categories: set[str], before: FeedCursor | None, limit: int
    ) -> list[ActivityRow]:
        branches = self._branches(categories)
        if not branches:
            return []
        union = "\nUNION ALL\n".join(branches)
        before_ts = before[0] if before is not None else None
        before_id = before[1] if before is not None else None
        # Keyset over the whole union on the total order (ts DESC, id DESC). The row-value compare
        # `(ts, id) < ($1, $2)` resumes strictly before the cursor; when there is no cursor the
        # `$1 IS NULL` guard short-circuits to the newest page. `id` is a uuid rendered as text, so
        # it is a stable, unique tiebreaker across the three source tables.
        sql = f"""
            SELECT id, category, kind, ts, title, snippet, ref, parent_ref, status, source
            FROM (
            {union}
            ) feed
            WHERE $1::timestamptz IS NULL OR (ts, id) < ($1::timestamptz, $2::text)
            ORDER BY ts DESC, id DESC
            LIMIT $3
        """
        async with self._db.acquire() as conn:
            rows = await conn.fetch(sql, before_ts, before_id, limit)
        return [_row(row) for row in rows]
