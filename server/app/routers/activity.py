"""Activity router (03-api.md §Activity feed).

``GET /activity`` is the **M8** merged, categorized feed (ADR-053 §4/§5): a UNION-of-views
projection over ``agent_runs`` + ``captures`` + ``review_queue`` (no new events table), each row
normalized to ``{id, category, kind, ts, title, snippet, ref}`` (+ ``parent_ref`` for pipeline
parent→child nesting), newest first, **keyset-paginated on ``(ts, id)``** via the opaque ``before=``
cursor. Category is by *origin* not table — a hand-run job lands under ``manual_actions`` via the
M8 ``agent_runs.trigger`` column. The projection + cursor logic live in
:mod:`app.services.activity_feed` (rule 5).

``GET /activity/runs/{id}`` returns one ``agent_runs`` row — status + ``details`` counts + the
human-readable summary. Implemented in **M2** (pulled forward from the M4 feed) so the Admin tab
can poll a reindex / tags-apply run to show its live counts.

``GET /activity/runs/{id}/logs`` is the **M8** live log tail (ADR-053 §1/§2): a cursor-paginated
(`?after_seq=`) read over ``agent_run_logs`` + a ``running`` flag so the client polls ~1s only while
the run is active and stops when it isn't. **Poll, not stream** (SSE is backlog).

Session-gated; the ``uuid.UUID`` path type yields ``422`` on a malformed id and ``404`` when the run
is unknown.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..config import Settings
from ..dependencies import (
    get_agent_run_store,
    get_run_log_store,
    get_settings,
    require_session,
)
from ..models import AgentRunResponse, RunChildModel, RunLogLineModel, RunLogsResponse
from ..services.activity_feed import (
    FEED_DEFAULT_LIMIT,
    ActivityCategory,
    ActivityFeedService,
    ActivityRow,
    InvalidActivityCursor,
    PgActivityFeedStore,
)
from ..services.agent_runs import RUNNING, AgentRunStore
from ..services.run_logs import RunLogStore

router = APIRouter(prefix="/activity", tags=["activity"], dependencies=[Depends(require_session)])


# The feed response models live in the router (not `models.py`) to keep this M8-batch task's edits
# to files it owns exclusively. They are wire contract only (no ORM), matching `models.py` style.
class ActivityFeedItem(BaseModel):
    """One normalized row of the merged feed (03-api §Activity). ``ref`` is the drill-down target
    (a run id → ``GET /activity/runs/{id}``, a chat-session id → the conversation, a review id);
    ``parent_ref`` links a pipeline step child to its parent run so the client nests them (null
    otherwise). ``title``/``snippet`` are null where the source row has none (a running run has no
    summary yet; a capture has no title until organized). ``status`` is the source row's lifecycle
    status; ``source`` (M8.1, ADR-054 §4) is a Captures row's origin badge
    (``text``/``voice``/``mcp``/``chat``), null on the non-capture branches."""

    id: str
    category: str
    kind: str
    ts: datetime
    title: str | None = None
    snippet: str | None = None
    ref: str | None = None
    parent_ref: str | None = None
    status: str | None = None
    source: str | None = None

    @classmethod
    def from_row(cls, row: ActivityRow) -> ActivityFeedItem:
        return cls(
            id=row.id,
            category=row.category,
            kind=row.kind,
            ts=row.ts,
            title=row.title,
            snippet=row.snippet,
            ref=row.ref,
            parent_ref=row.parent_ref,
            status=row.status,
            source=row.source,
        )


class ActivityFeedResponse(BaseModel):
    """GET /activity — one keyset page. ``next_before`` is the opaque cursor to pass back as
    ``before=`` for the following (older) page; ``None`` at the end of the feed."""

    items: list[ActivityFeedItem] = Field(default_factory=list)
    next_before: str | None = None


def get_activity_feed_service(request: Request) -> ActivityFeedService:
    """Build the feed service over the shared DB pool. Constructed per request (the store is a thin
    stateless asyncpg wrapper) rather than an ``app.state`` singleton, since this M8-batch task adds
    no ``main.py`` lifespan wiring; the coordinator may later promote it to ``app.state``."""
    return ActivityFeedService(PgActivityFeedStore(request.app.state.db))


@router.get("", response_model=ActivityFeedResponse)
async def get_activity(
    category: ActivityCategory | None = Query(
        default=None, description="agents_jobs | conversations | manual_actions; all when omitted"
    ),
    limit: int = Query(default=FEED_DEFAULT_LIMIT, ge=1),
    before: str | None = Query(default=None, description="opaque keyset cursor from next_before"),
    service: ActivityFeedService = Depends(get_activity_feed_service),
) -> ActivityFeedResponse:
    """The merged categorized activity feed (ADR-053 §4). ``category`` narrows to one tab (unknown
    value → 422 via the Literal type); ``limit`` is clamped to the service cap; a malformed
    ``before`` cursor → 422. Newest first, keyset-paginated on ``(ts, id)``."""
    try:
        page = await service.feed(category=category, before=before, limit=limit)
    except InvalidActivityCursor:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid cursor"
        ) from None
    return ActivityFeedResponse(
        items=[ActivityFeedItem.from_row(row) for row in page.items],
        next_before=page.next_before,
    )


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def get_run(
    run_id: uuid.UUID,
    store: AgentRunStore = Depends(get_agent_run_store),
) -> AgentRunResponse:
    run = await store.get(str(run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    # The recursive step subtree (M8.1, ADR-054 §2) — empty for a leaf run, the full pipeline tree
    # for a parent. Only fetched here, on the drill-down, never in the flat feed.
    children = await store.children_tree(str(run_id))
    return AgentRunResponse(
        id=run.id,
        agent=run.agent,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        model_used=run.model_used,
        fallback_used=run.fallback_used,
        summary=run.summary,
        details=run.details,
        error=run.error,
        trigger=run.trigger,
        children=[RunChildModel.from_run_child(c) for c in children],
    )


@router.get("/runs/{run_id}/logs", response_model=RunLogsResponse)
async def get_run_logs(
    run_id: uuid.UUID,
    after_seq: int = Query(0, ge=0),
    store: AgentRunStore = Depends(get_agent_run_store),
    log_store: RunLogStore = Depends(get_run_log_store),
    settings: Settings = Depends(get_settings),
) -> RunLogsResponse:
    # The run must exist (404) — reading its status also gives the `running` flag that tells the
    # client when to stop polling. Reads the run first so an unknown id never returns empty-200.
    run = await store.get(str(run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    lines = await log_store.read_after(
        str(run_id), after_seq=after_seq, limit=settings.run_log_tail_max_lines
    )
    next_after_seq = lines[-1].seq if lines else after_seq
    return RunLogsResponse(
        run_id=run.id,
        running=run.status == RUNNING,
        logs=[
            RunLogLineModel(seq=line.seq, ts=line.ts, level=line.level, message=line.message)
            for line in lines
        ],
        next_after_seq=next_after_seq,
    )
