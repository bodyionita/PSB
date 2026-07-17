"""Activity router tests: GET /activity/runs/{id} over a fake AgentRunStore (no DB, auth bypassed).

Covers the Admin-tab run-status poll: a known run returns status + details counts, an unknown
(well-formed) id → 404, a malformed id → 422 (uuid path type).
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import (
    get_agent_run_store,
    get_run_log_store,
    get_settings,
    require_session,
)
from app.routers import activity
from app.services.agent_runs import RUNNING, SUCCEEDED, AgentRun
from app.services.run_logs import RunLogLine

from .fakes import FakeAgentRunStore, FakeRunLogStore

PREFIX = "/api/v1"


def _client(store: FakeAgentRunStore, *, log_store: FakeRunLogStore | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(activity.router, prefix=PREFIX)
    app.dependency_overrides[get_agent_run_store] = lambda: store
    app.dependency_overrides[get_run_log_store] = lambda: log_store or FakeRunLogStore()
    app.dependency_overrides[get_settings] = lambda: Settings()
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


def test_get_run_reports_trigger():
    store = FakeAgentRunStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(id=run_id, agent="reindex", status=SUCCEEDED, trigger="manual")
    body = _client(store).get(f"{PREFIX}/activity/runs/{run_id}").json()
    assert body["trigger"] == "manual"


# --- GET /activity/runs/{id}/logs — the M8 live log tail (poll) --------------------------------


def _seed_logs(log_store: FakeRunLogStore, run_id: str, n: int) -> None:
    from datetime import UTC, datetime

    for seq in range(1, n + 1):  # 1-based, mirroring the buffer's ordinals
        log_store.lines.setdefault(run_id, {})[seq] = RunLogLine(
            seq=seq, ts=datetime.now(UTC), level="INFO", message=f"line {seq}"
        )


def test_logs_returns_lines_running_flag_and_cursor():
    store = FakeAgentRunStore()
    log_store = FakeRunLogStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(id=run_id, agent="reindex", status=RUNNING)
    _seed_logs(log_store, run_id, 3)

    # The default after_seq=0 cursor returns the whole tail (1-based seq → the first line included).
    body = _client(store, log_store=log_store).get(f"{PREFIX}/activity/runs/{run_id}/logs").json()
    assert body["running"] is True
    assert [line["seq"] for line in body["logs"]] == [1, 2, 3]
    assert body["logs"][0]["message"] == "line 1"
    assert body["next_after_seq"] == 3


def test_logs_after_seq_pages_and_stops_when_not_running():
    store = FakeAgentRunStore()
    log_store = FakeRunLogStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(id=run_id, agent="reindex", status=SUCCEEDED)
    _seed_logs(log_store, run_id, 5)

    body = (
        _client(store, log_store=log_store)
        .get(f"{PREFIX}/activity/runs/{run_id}/logs", params={"after_seq": 3})
        .json()
    )
    assert [line["seq"] for line in body["logs"]] == [4, 5]
    assert body["running"] is False  # the client stops polling
    # No new lines beyond the cursor → next_after_seq is unchanged (stays the request cursor).
    empty = (
        _client(store, log_store=log_store)
        .get(f"{PREFIX}/activity/runs/{run_id}/logs", params={"after_seq": 5})
        .json()
    )
    assert empty["logs"] == []
    assert empty["next_after_seq"] == 5


def test_logs_unknown_run_404():
    resp = _client(FakeAgentRunStore()).get(f"{PREFIX}/activity/runs/{uuid.uuid4()}/logs")
    assert resp.status_code == 404


def test_logs_malformed_id_422():
    resp = _client(FakeAgentRunStore()).get(f"{PREFIX}/activity/runs/not-a-uuid/logs")
    assert resp.status_code == 422


def test_logs_negative_after_seq_422():
    resp = _client(FakeAgentRunStore()).get(
        f"{PREFIX}/activity/runs/{uuid.uuid4()}/logs", params={"after_seq": -1}
    )
    assert resp.status_code == 422
