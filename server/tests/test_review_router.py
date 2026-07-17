"""Review router tests: GET /review + POST /review/{id} wiring + status-code mapping.

The resolution business logic is covered in test_review_service; here we assert the router
delegates, serialises the record, and maps the service errors to 404 / 409 / 400.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_review_service, get_settings, require_session
from app.routers import review
from app.services.review_queue import KIND_ENTITY_AMBIGUITY, ReviewRecord
from app.services.review_service import (
    BadResolution,
    BatchItemResult,
    ReviewNotFound,
    ReviewNotPending,
)

PREFIX = "/api/v1"
NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
REVIEW_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _record(review_id: str = REVIEW_ID, status: str = "pending") -> ReviewRecord:
    return ReviewRecord(
        id=review_id,
        kind=KIND_ENTITY_AMBIGUITY,
        payload={"mention": {"name": "Alex"}, "candidates": [{"id": "cand-a"}]},
        excerpt="met Alex",
        source="text",
        source_ref="cap-1",
        status=status,
        resolution=None,
        created_at=NOW,
    )


class FakeReviewService:
    def __init__(self) -> None:
        self.list_args: dict | None = None
        self.resolve_args: dict | None = None
        self.batch_args: dict | None = None
        self.get_args: dict | None = None
        self.get_result: ReviewRecord | None = _record(REVIEW_ID, status="resolved")
        self.raises: Exception | None = None

    async def list_items(self, *, status=None, kind=None):
        self.list_args = {"status": status, "kind": kind}
        return [_record()]

    async def get_item(self, review_id):
        self.get_args = {"review_id": review_id}
        return self.get_result

    async def resolve(self, review_id, *, choice=None, verdict=None, action=None, survivor=None):
        self.resolve_args = {
            "review_id": review_id,
            "choice": choice,
            "verdict": verdict,
            "action": action,
            "survivor": survivor,
        }
        if self.raises is not None:
            raise self.raises
        return _record(review_id, status="resolved")

    async def resolve_batch(self, ids, action):
        self.batch_args = {"ids": ids, "action": action}
        return [BatchItemResult(id=i, ok=True) for i in ids]


def _make_client(fake: FakeReviewService, settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(review.router, prefix=PREFIX)
    app.dependency_overrides[get_review_service] = lambda: fake
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


@pytest.fixture
def client_and_service():
    fake = FakeReviewService()
    return _make_client(fake, Settings()), fake


def test_list_defaults_status_pending(client_and_service):
    client, fake = client_and_service
    resp = client.get(f"{PREFIX}/review")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == REVIEW_ID and body[0]["status"] == "pending"
    assert fake.list_args == {"status": "pending", "kind": None}


def test_list_passes_filters(client_and_service):
    client, fake = client_and_service
    client.get(f"{PREFIX}/review", params={"status": "all", "kind": "vocab-proposal"})
    assert fake.list_args == {"status": "all", "kind": "vocab-proposal"}


def test_get_review_returns_item_any_status(client_and_service):
    # GET /review/{id} serialises the record (a resolved one here — the Activity "Reviewed" expand).
    client, fake = client_and_service
    resp = client.get(f"{PREFIX}/review/{REVIEW_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == REVIEW_ID and body["status"] == "resolved"
    assert fake.get_args == {"review_id": REVIEW_ID}


def test_get_review_unknown_is_404(client_and_service):
    client, fake = client_and_service
    fake.get_result = None
    resp = client.get(f"{PREFIX}/review/{REVIEW_ID}")
    assert resp.status_code == 404


def test_get_review_malformed_id_is_422(client_and_service):
    client, _ = client_and_service
    resp = client.get(f"{PREFIX}/review/not-a-uuid")
    assert resp.status_code == 422


def test_resolve_delegates_and_serialises(client_and_service):
    client, fake = client_and_service
    resp = client.post(f"{PREFIX}/review/{REVIEW_ID}", json={"choice": "cand-a"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert fake.resolve_args == {
        "review_id": REVIEW_ID,
        "choice": "cand-a",
        "verdict": None,
        "action": None,
        "survivor": None,
    }


def test_resolve_passes_dedup_action_and_survivor(client_and_service):
    # A dedup-proposal resolution forwards `action` + `survivor` through the router (ADR-049).
    client, fake = client_and_service
    resp = client.post(
        f"{PREFIX}/review/{REVIEW_ID}", json={"action": "merge", "survivor": "node-x"}
    )
    assert resp.status_code == 200
    assert fake.resolve_args["action"] == "merge"
    assert fake.resolve_args["survivor"] == "node-x"


def test_batch_delegates_and_serialises(client_and_service):
    client, fake = client_and_service
    ids = [REVIEW_ID, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"]
    resp = client.post(f"{PREFIX}/review/batch", json={"ids": ids, "action": "agree"})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body["results"]] == ids
    assert all(r["ok"] and r["error"] is None for r in body["results"])
    assert fake.batch_args == {"ids": ids, "action": "agree"}


def test_batch_route_not_shadowed_by_review_id(client_and_service):
    # /review/batch must hit the batch handler, not POST /review/{id} with id="batch".
    client, fake = client_and_service
    resp = client.post(f"{PREFIX}/review/batch", json={"ids": [REVIEW_ID], "action": "maybe"})
    assert resp.status_code == 200
    assert fake.batch_args is not None and fake.resolve_args is None


def test_batch_empty_ids_is_422(client_and_service):
    client, _ = client_and_service
    resp = client.post(f"{PREFIX}/review/batch", json={"ids": [], "action": "agree"})
    assert resp.status_code == 422


def test_batch_malformed_id_is_422(client_and_service):
    client, _ = client_and_service
    resp = client.post(f"{PREFIX}/review/batch", json={"ids": ["not-a-uuid"], "action": "agree"})
    assert resp.status_code == 422


def test_batch_over_cap_is_422_before_any_resolve():
    # A batch larger than review_batch_max is rejected wholesale (422) before any side effect.
    fake = FakeReviewService()
    client = _make_client(fake, Settings(review_batch_max=1))
    ids = [REVIEW_ID, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"]
    resp = client.post(f"{PREFIX}/review/batch", json={"ids": ids, "action": "agree"})
    assert resp.status_code == 422
    assert fake.batch_args is None  # never delegated


def test_resolve_malformed_id_is_422(client_and_service):
    client, _ = client_and_service
    resp = client.post(f"{PREFIX}/review/not-a-uuid", json={"choice": "cand-a"})
    assert resp.status_code == 422


def test_resolve_unknown_is_404(client_and_service):
    client, fake = client_and_service
    fake.raises = ReviewNotFound(REVIEW_ID)
    resp = client.post(f"{PREFIX}/review/{REVIEW_ID}", json={"choice": "cand-a"})
    assert resp.status_code == 404


def test_resolve_already_resolved_is_409(client_and_service):
    client, fake = client_and_service
    fake.raises = ReviewNotPending(REVIEW_ID)
    resp = client.post(f"{PREFIX}/review/{REVIEW_ID}", json={"choice": "cand-a"})
    assert resp.status_code == 409


def test_resolve_bad_body_is_400(client_and_service):
    client, fake = client_and_service
    fake.raises = BadResolution("entity-ambiguity requires a 'choice'")
    resp = client.post(f"{PREFIX}/review/{REVIEW_ID}", json={})
    assert resp.status_code == 400
