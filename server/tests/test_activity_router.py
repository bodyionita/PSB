"""Activity router tests: GET /activity/runs/{id} over a fake AgentRunStore (no DB, auth bypassed).

Covers the Admin-tab run-status poll: a known run returns status + details counts, an unknown
(well-formed) id → 404, a malformed id → 422 (uuid path type).
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_agent_run_store, require_session
from app.routers import activity
from app.services.agent_runs import RUNNING, SUCCEEDED, AgentRun

from .fakes import FakeAgentRunStore

PREFIX = "/api/v1"


def _client(store: FakeAgentRunStore) -> TestClient:
    app = FastAPI()
    app.include_router(activity.router, prefix=PREFIX)
    app.dependency_overrides[get_agent_run_store] = lambda: store
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def test_get_run_returns_status_and_details():
    store = FakeAgentRunStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(
        id=run_id,
        agent="reindex",
        status=SUCCEEDED,
        summary="reindexed 3 notes",
        details={"indexed": 3, "skipped": 1, "deleted": 0, "failed": 0, "partial": False},
    )
    resp = _client(store).get(f"{PREFIX}/activity/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == run_id
    assert body["agent"] == "reindex"
    assert body["status"] == "succeeded"
    assert body["details"]["indexed"] == 3
    assert body["summary"] == "reindexed 3 notes"


def test_get_run_in_progress_has_no_finished_at():
    store = FakeAgentRunStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(id=run_id, agent="reindex", status=RUNNING)
    body = _client(store).get(f"{PREFIX}/activity/runs/{run_id}").json()
    assert body["status"] == "running"
    assert body["finished_at"] is None


def test_get_run_unknown_id_404():
    store = FakeAgentRunStore()
    resp = _client(store).get(f"{PREFIX}/activity/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_run_malformed_id_422():
    resp = _client(FakeAgentRunStore()).get(f"{PREFIX}/activity/runs/not-a-uuid")
    assert resp.status_code == 422
