"""Agents roster + manual trigger (03-api.md §Activity & ops, M8 task 3, ADR-053 §6/§7).

``GET /agents`` — a **flat roster** of individual jobs: each ``{name, category, pipelines: [names],
running, last_run}``. A job's schedule is *derived* from its 0..N pipeline memberships
(many-to-many, ADR-053 §6); schedule detail lives on ``GET /pipelines``.

``POST /agents/{name}/run`` — manually trigger one **zero-arg** job standalone (invariant 4), over
the T1 **JobRunner** single-flight guard, stamped ``trigger=manual``. ``404`` unknown job, ``409``
if that agent — or a live pipeline it is a step of — is already running, ``503`` if the scheduler
(the runnable set) isn't live on this instance. The run is **backgrounded** (``202``); the ops
console tails it via this roster's ``last_run.run_id`` → ``GET /activity/runs/{id}/logs``.

Session-gated, like every operational surface.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..dependencies import require_session
from ..services.job_runner import JobAlreadyRunning
from ..services.roster import (
    AgentEntry,
    RosterService,
    SchedulerUnavailable,
    UnknownAgent,
)

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_session)])


def get_roster_service(request: Request) -> RosterService:
    """Build the roster over the live ``app.state`` singletons (the running scheduler, the
    ``agent_runs`` store, the shared JobRunner). Per-request + stateless, so it always reflects the
    current scheduler; kept off ``dependencies.py`` to stay within this task's file scope. Reused by
    the pipelines router."""
    state = request.app.state
    return RosterService(
        scheduler=getattr(state, "scheduler", None),
        run_store=state.agent_run_store,
        job_runner=state.job_runner,
        settings=state.settings,
    )


class LastRunModel(BaseModel):
    """The most recent run for a roster entry (``null`` when it has never run)."""

    status: str
    finished_at: datetime | None = None
    run_id: str


class AgentRosterItem(BaseModel):
    """One flat-roster row (ADR-053 §6). ``running`` is live single-flight status from the shared
    JobRunner (the seam T1 exposed for this roster)."""

    name: str
    category: str
    pipelines: list[str] = Field(default_factory=list)
    running: bool
    last_run: LastRunModel | None = None


class AgentRunTriggered(BaseModel):
    """``202`` acknowledgement — the named job was triggered and runs in the background (poll its
    ``last_run.run_id`` here, then the run-logs tail)."""

    agent: str


def _item(entry: AgentEntry) -> AgentRosterItem:
    last = entry.last_run
    return AgentRosterItem(
        name=entry.name,
        category=entry.category,
        pipelines=entry.pipelines,
        running=entry.running,
        last_run=(
            None
            if last is None
            else LastRunModel(status=last.status, finished_at=last.finished_at, run_id=last.run_id)
        ),
    )


@router.get("", response_model=list[AgentRosterItem])
async def list_agents(
    roster: RosterService = Depends(get_roster_service),
) -> list[AgentRosterItem]:
    return [_item(entry) for entry in await roster.agents()]


@router.post(
    "/{name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunTriggered,
)
async def run_agent(
    name: str,
    roster: RosterService = Depends(get_roster_service),
) -> AgentRunTriggered:
    try:
        await roster.trigger_agent(name)
    except UnknownAgent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown agent {name!r}"
        ) from None
    except JobAlreadyRunning:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"agent {name!r} is already running"
        ) from None
    except SchedulerUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the scheduler is not running on this instance",
        ) from None
    return AgentRunTriggered(agent=name)
