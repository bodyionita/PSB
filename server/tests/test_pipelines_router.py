"""Pipelines router tests (M8 task 3): GET /pipelines projection + POST /pipelines/{name}/run
status mapping. A fake roster drives each branch (auth bypassed, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import require_session
from app.routers import pipelines
from app.routers.agents import get_roster_service
from app.services.job_runner import JobAlreadyRunning
from app.services.roster import LastRun, PipelineEntry, SchedulerUnavailable, UnknownPipeline

PREFIX = "/api/v1"


class _FakeRoster:
    def __init__(self, *, pipeline_entries=None, raise_on_pipeline=None) -> None:
        self._pipelines = pipeline_entries or []
        self._raise = raise_on_pipeline
        self.triggered: list[str] = []

    async def pipelines(self):
        return self._pipelines

    async def trigger_pipeline(self, name: str) -> None:
        self.triggered.append(name)
        if self._raise is not None:
            raise self._raise


def _client(roster: _FakeRoster) -> TestClient:
    app = FastAPI()
    app.include_router(pipelines.router, prefix=PREFIX)
    app.dependency_overrides[get_roster_service] = lambda: roster
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_get_pipelines_projects_resource():
    when = datetime(2026, 7, 18, 3, 0, tzinfo=UTC)
    entries = [
        PipelineEntry(
            name="nightly",
            cron="0 3 * * *",
            next_run=when,
            steps=["reindex", "dedup-sweep"],
            last_run=LastRun(status="running", finished_at=None, run_id="p1"),
        ),
        PipelineEntry(
            name="weekly",
            cron="30 4 * * sun",
            next_run=None,
            steps=["integrity-drill"],
            last_run=None,
        ),
    ]
    body = _client(_FakeRoster(pipeline_entries=entries)).get(f"{PREFIX}/pipelines").json()
    assert [p["name"] for p in body] == ["nightly", "weekly"]
    assert body[0]["cron"] == "0 3 * * *"
    assert body[0]["steps"] == ["reindex", "dedup-sweep"]
    assert body[0]["next_run"].startswith("2026-07-18T03:00:00")
    assert body[0]["last_run"]["run_id"] == "p1"
    assert body[1]["next_run"] is None
    assert body[1]["last_run"] is None


def test_run_pipeline_accepted_202():
    roster = _FakeRoster()
    resp = _client(roster).post(f"{PREFIX}/pipelines/nightly/run")
    assert resp.status_code == 202
    assert resp.json() == {"pipeline": "nightly"}
    assert roster.triggered == ["nightly"]


def test_run_pipeline_unknown_404():
    roster = _FakeRoster(raise_on_pipeline=UnknownPipeline("nope"))
    resp = _client(roster).post(f"{PREFIX}/pipelines/nope/run")
    assert resp.status_code == 404


def test_run_pipeline_conflict_409():
    roster = _FakeRoster(raise_on_pipeline=JobAlreadyRunning("nightly"))
    resp = _client(roster).post(f"{PREFIX}/pipelines/nightly/run")
    assert resp.status_code == 409


def test_run_pipeline_scheduler_unavailable_503():
    roster = _FakeRoster(raise_on_pipeline=SchedulerUnavailable("nightly"))
    resp = _client(roster).post(f"{PREFIX}/pipelines/nightly/run")
    assert resp.status_code == 503
