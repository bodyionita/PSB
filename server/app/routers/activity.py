"""Activity router (03-api.md §Activity feed).

``GET /activity/runs/{id}`` returns one ``agent_runs`` row — status + ``details`` counts + the
human-readable summary. Implemented in **M2** (pulled forward from the M4 feed) so the Admin tab
can poll a reindex / tags-apply run to show its live counts; the merged ``GET /activity`` list
stays M4. Session-gated; the ``uuid.UUID`` path type yields ``422`` on a malformed id and ``404``
when the run is unknown.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import get_agent_run_store, require_session
from ..models import AgentRunResponse
from ..services.agent_runs import AgentRunStore

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
    )
