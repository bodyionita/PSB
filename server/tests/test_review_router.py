"""Review router tests: GET /review + POST /review/{id} wiring + status-code mapping.

The resolution business logic is covered in test_review_service; here we assert the router
delegates, serialises the record, and maps the service errors to 404 / 409 / 400.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_review_service, require_session
from app.routers import review
from app.services.review_queue import KIND_ENTITY_AMBIGUITY, ReviewRecord
from app.services.review_service import BadResolution, ReviewNotFound, ReviewNotPending

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
        self.raises: Exception | None = None

    async def list_items(self, *, status=None, kind=None):
        self.list_args = {"status": status, "kind": kind}
        return [_record()]

    async def resolve(self, review_id, *, choice=None, verdict=None):
        self.resolve_args = {"review_id": review_id, "choice": choice, "verdict": verdict}
        if self.raises is not None:
            raise self.raises
        return _record(review_id, status="resolved")


@pytest.fixture
def client_and_service():
    app = FastAPI()
    app.include_router(review.router, prefix=PREFIX)
    fake = FakeReviewService()
    app.dependency_overrides[get_review_service] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), fake


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


def test_resolve_delegates_and_serialises(client_and_service):
    client, fake = client_and_service
    resp = client.post(f"{PREFIX}/review/{REVIEW_ID}", json={"choice": "cand-a"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert fake.resolve_args == {"review_id": REVIEW_ID, "choice": "cand-a", "verdict": None}


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
