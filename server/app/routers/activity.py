"""Activity router (03-api.md §Activity feed).

``GET /activity/runs/{id}`` returns one ``agent_runs`` row — status + ``details`` counts + the
human-readable summary. Implemented in **M2** (pulled forward from the M4 feed) so the Admin tab
can poll a reindex / tags-apply run to show its live counts; the merged ``GET /activity`` list
stays M4/M8.

``GET /activity/runs/{id}/logs`` is the **M8** live log tail (ADR-053 §1/§2): a cursor-paginated
(`?after_seq=`) read over ``agent_run_logs`` + a ``running`` flag so the client polls ~1s only while
the run is active and stops when it isn't. **Poll, not stream** (SSE is backlog).

Session-gated; the ``uuid.UUID`` path type yields ``422`` on a malformed id and ``404`` when the run
is unknown.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..config import Settings
from ..dependencies import (
    get_agent_run_store,
    get_run_log_store,
    get_settings,
    require_session,
)
from ..models import AgentRunResponse, RunLogLineModel, RunLogsResponse
from ..services.agent_runs import RUNNING, AgentRunStore
from ..services.run_logs import RunLogStore

router = APIRouter(prefix="/activity", tags=["activity"], dependencies=[Depends(require_session)])


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def get_run(
    run_id: uuid.UUID,
    store: AgentRunStore = Depends(get_agent_run_store),
) -> AgentRunResponse:
    run = await store.get(str(run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
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
