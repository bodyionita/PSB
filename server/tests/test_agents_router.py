"""Agents router tests (M8 task 3): GET /agents projection + POST /agents/{name}/run status mapping.

The router's job is validation + delegation + exception→HTTP mapping; the single-flight/manual-scope
behaviour lives in test_roster.py. Here a fake roster drives each branch (auth bypassed, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import require_session
from app.routers import agents
from app.routers.agents import get_roster_service
from app.services.job_runner import JobAlreadyRunning
from app.services.roster import AGENTS_JOBS, AgentEntry, LastRun, SchedulerUnavailable, UnknownAgent

PREFIX = "/api/v1"


class _FakeRoster:
    def __init__(self, *, agent_entries=None, raise_on_agent=None) -> None:
        self._agents = agent_entries or []
        self._raise = raise_on_agent
        self.triggered: list[str] = []

    async def agents(self):
        return self._agents

    async def trigger_agent(self, name: str) -> None:
        self.triggered.append(name)
        if self._raise is not None:
            raise self._raise


def _client(roster: _FakeRoster) -> TestClient:
    app = FastAPI()
    app.include_router(agents.router, prefix=PREFIX)
    app.dependency_overrides[get_roster_service] = lambda: roster
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_get_agents_projects_roster():
    entries = [
        AgentEntry(
            name="reindex",
            category=AGENTS_JOBS,
            pipelines=["nightly", "weekly"],
            running=True,
            last_run=LastRun(status="succeeded", finished_at=datetime.now(UTC), run_id="r1"),
        ),
        AgentEntry(
            name="dedup-sweep",
            category=AGENTS_JOBS,
            pipelines=["nightly"],
            running=False,
            last_run=None,
        ),
    ]
    body = _client(_FakeRoster(agent_entries=entries)).get(f"{PREFIX}/agents").json()
    assert [a["name"] for a in body] == ["reindex", "dedup-sweep"]
    assert body[0]["category"] == AGENTS_JOBS
    assert body[0]["pipelines"] == ["nightly", "weekly"]
    assert body[0]["running"] is True
    assert body[0]["last_run"]["status"] == "succeeded"
    assert body[0]["last_run"]["run_id"] == "r1"
    assert body[1]["last_run"] is None


def test_run_agent_accepted_202():
    roster = _FakeRoster()
    resp = _client(roster).post(f"{PREFIX}/agents/reindex/run")
    assert resp.status_code == 202
    assert resp.json() == {"agent": "reindex"}
    assert roster.triggered == ["reindex"]


def test_run_agent_unknown_404():
    roster = _FakeRoster(raise_on_agent=UnknownAgent("nope"))
    resp = _client(roster).post(f"{PREFIX}/agents/nope/run")
    assert resp.status_code == 404


def test_run_agent_conflict_409():
    roster = _FakeRoster(raise_on_agent=JobAlreadyRunning("reindex"))
    resp = _client(roster).post(f"{PREFIX}/agents/reindex/run")
    assert resp.status_code == 409


def test_run_agent_scheduler_unavailable_503():
    roster = _FakeRoster(raise_on_agent=SchedulerUnavailable("reindex"))
    resp = _client(roster).post(f"{PREFIX}/agents/reindex/run")
    assert resp.status_code == 503
