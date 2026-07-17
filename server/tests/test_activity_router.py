"""Activity router tests: GET /activity/runs/{id} over a fake AgentRunStore (no DB, auth bypassed).

Covers the Admin-tab run-status poll: a known run returns status + details counts, an unknown
(well-formed) id → 404, a malformed id → 422 (uuid path type).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
from app.services.activity_feed import (
    CATEGORY_AGENTS_JOBS,
    CATEGORY_CAPTURES,
    CATEGORY_MANUAL_ACTIONS,
    ActivityFeedService,
    ActivityRow,
)
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


def test_get_run_leaf_has_empty_children():
    store = FakeAgentRunStore()
    run_id = str(uuid.uuid4())
    store.runs[run_id] = AgentRun(id=run_id, agent="reindex", status=SUCCEEDED)
    body = _client(store).get(f"{PREFIX}/activity/runs/{run_id}").json()
    assert body["children"] == []


def test_get_run_returns_recursive_children_tree():
    # A parent → two step children → one grandchild under stepA. The detail must render the tree,
    # siblings early→late, with the grandchild nested one level deeper (M8.1, ADR-054 §2).
    store = FakeAgentRunStore()
    parent = str(uuid.uuid4())
    step_a = str(uuid.uuid4())
    step_b = str(uuid.uuid4())
    grand = str(uuid.uuid4())
    t0 = datetime(2026, 7, 17, 3, 0, 0, tzinfo=UTC)
    store.runs[parent] = AgentRun(id=parent, agent="nightly", status=SUCCEEDED, started_at=t0)
    store.runs[step_a] = AgentRun(
        id=step_a,
        agent="chat-distill",
        status=SUCCEEDED,
        started_at=t0 + timedelta(seconds=1),
        summary="distilled",
        parent_run_id=parent,
    )
    store.runs[grand] = AgentRun(
        id=grand,
        agent="capture",
        status="failed",
        started_at=t0 + timedelta(seconds=2),
        parent_run_id=step_a,
    )
    store.runs[step_b] = AgentRun(
        id=step_b,
        agent="reindex",
        status=SUCCEEDED,
        started_at=t0 + timedelta(seconds=3),
        parent_run_id=parent,
    )
    body = _client(store).get(f"{PREFIX}/activity/runs/{parent}").json()
    children = body["children"]
    assert [c["name"] for c in children] == ["chat-distill", "reindex"]  # early→late
    assert children[0]["summary"] == "distilled"
    grand_nodes = children[0]["children"]
    assert [g["name"] for g in grand_nodes] == ["capture"]
    assert grand_nodes[0]["status"] == "failed"
    assert children[1]["children"] == []


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


# --- GET /activity — the merged categorized feed (M8, ADR-053 §4/§5) ---------------------------

_FEED_BASE = datetime(2026, 7, 17, 9, 0, 0, tzinfo=UTC)


class _FakeFeedStore:
    """Mirrors PgActivityFeedStore.read: category filter + keyset on (ts, id) + ts/id-desc + limit.
    Wrapped by a real ActivityFeedService so the router exercises the true cursor logic (no DB)."""

    def __init__(self, rows: list[ActivityRow]) -> None:
        self._rows = list(rows)

    async def read(self, *, categories, before, limit):
        rows = [r for r in self._rows if r.category in categories]
        rows.sort(key=lambda r: (r.ts, r.id), reverse=True)
        if before is not None:
            rows = [r for r in rows if (r.ts, r.id) < before]
        return rows[:limit]


def _feed_row(id: str, category: str, *, n: int, parent_ref: str | None = None) -> ActivityRow:
    return ActivityRow(
        id=id,
        category=category,
        kind="agent_run",
        ts=_FEED_BASE + timedelta(seconds=n),
        title=f"title-{id}",
        snippet=f"snippet-{id}",
        ref=id,
        parent_ref=parent_ref,
    )


def _feed_client(rows: list[ActivityRow]) -> TestClient:
    app = FastAPI()
    app.include_router(activity.router, prefix=PREFIX)
    app.dependency_overrides[activity.get_activity_feed_service] = lambda: ActivityFeedService(
        _FakeFeedStore(rows)
    )
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def test_feed_returns_normalized_rows_newest_first():
    rows = [
        _feed_row("a", CATEGORY_AGENTS_JOBS, n=1),
        _feed_row("c", CATEGORY_CAPTURES, n=2),
        _feed_row("m", CATEGORY_MANUAL_ACTIONS, n=3, parent_ref="parent-1"),
    ]
    body = _feed_client(rows).get(f"{PREFIX}/activity").json()
    assert [i["id"] for i in body["items"]] == ["m", "c", "a"]  # ts desc
    first = body["items"][0]
    assert set(first) == {
        "id",
        "category",
        "kind",
        "ts",
        "title",
        "snippet",
        "ref",
        "parent_ref",
        "status",
        "source",
    }
    assert first["category"] == CATEGORY_MANUAL_ACTIONS
    assert first["parent_ref"] == "parent-1"
    assert body["next_before"] is None  # whole feed fit in one page


def test_feed_category_filter():
    rows = [
        _feed_row("a", CATEGORY_AGENTS_JOBS, n=1),
        _feed_row("c", CATEGORY_CAPTURES, n=2),
        _feed_row("m", CATEGORY_MANUAL_ACTIONS, n=3),
    ]
    body = (
        _feed_client(rows).get(f"{PREFIX}/activity", params={"category": CATEGORY_CAPTURES}).json()
    )
    assert [i["id"] for i in body["items"]] == ["c"]


def test_feed_unknown_category_422():
    resp = _feed_client([]).get(f"{PREFIX}/activity", params={"category": "bogus"})
    assert resp.status_code == 422


def test_feed_keyset_pagination_via_next_before():
    rows = [_feed_row(f"r{n}", CATEGORY_AGENTS_JOBS, n=n) for n in range(4)]
    client = _feed_client(rows)

    first = client.get(f"{PREFIX}/activity", params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == ["r3", "r2"]
    assert first["next_before"]

    second = client.get(
        f"{PREFIX}/activity", params={"limit": 2, "before": first["next_before"]}
    ).json()
    assert [i["id"] for i in second["items"]] == ["r1", "r0"]
    assert second["next_before"] is None


def test_feed_invalid_before_cursor_422():
    resp = _feed_client([]).get(f"{PREFIX}/activity", params={"before": "!!!not-base64!!!"})
    assert resp.status_code == 422
