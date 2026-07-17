"""Pipelines as a first-class resource + whole-pipeline trigger (03-api.md §Activity & ops, M8 task
3, ADR-053 §6).

``GET /pipelines`` — each ``{name, cron, next_run, steps: [ordered names], last_run}``, sourced from
the **live scheduler** (``pipeline_runners()`` + APScheduler ``next_run_time``). A pipeline run is a
parent ``agent_runs`` row, each step a child (``parent_run_id``). Future home of pipeline *editing*
(deferred, ADR-053).

``POST /pipelines/{name}/run`` — manually trigger a **whole pipeline** (the ADR-047 §6 CLI verb over
HTTP) over the T1 JobRunner guard, stamped ``manual``; ``404`` unknown, ``409`` if already running,
``503`` if the scheduler isn't live here. Backgrounded (``202``).

Session-gated.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..dependencies import require_session
from ..services.job_runner import JobAlreadyRunning
from ..services.roster import (
    PipelineEntry,
    RosterService,
    SchedulerUnavailable,
    UnknownPipeline,
)
from .agents import LastRunModel, get_roster_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"], dependencies=[Depends(require_session)])


class PipelineItem(BaseModel):
    """One pipeline (ADR-053 §6): cadence, live next-run, ordered step names, last-run."""

    name: str
    cron: str
    next_run: datetime | None = None
    steps: list[str] = Field(default_factory=list)
    last_run: LastRunModel | None = None


class PipelineRunTriggered(BaseModel):
    """``202`` acknowledgement — the pipeline was triggered and runs in the background."""

    pipeline: str


def _item(entry: PipelineEntry) -> PipelineItem:
    last = entry.last_run
    return PipelineItem(
        name=entry.name,
        cron=entry.cron,
        next_run=entry.next_run,
        steps=entry.steps,
        last_run=(
            None
            if last is None
            else LastRunModel(status=last.status, finished_at=last.finished_at, run_id=last.run_id)
        ),
    )


@router.get("", response_model=list[PipelineItem])
async def list_pipelines(
    roster: RosterService = Depends(get_roster_service),
) -> list[PipelineItem]:
    return [_item(entry) for entry in await roster.pipelines()]


@router.post(
    "/{name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PipelineRunTriggered,
)
async def run_pipeline(
    name: str,
    roster: RosterService = Depends(get_roster_service),
) -> PipelineRunTriggered:
    try:
        await roster.trigger_pipeline(name)
    except UnknownPipeline:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown pipeline {name!r}"
        ) from None
    except JobAlreadyRunning:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"pipeline {name!r} is already running"
        ) from None
    except SchedulerUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the scheduler is not running on this instance",
        ) from None
    return PipelineRunTriggered(pipeline=name)
