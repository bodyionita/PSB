"""Capture router tests: FastAPI TestClient over a fake pipeline (no DB, no LLM, no background).

The router's job is validation + delegation + error translation (CLAUDE.md rule 5); the pipeline
logic itself is covered in ``test_capture_pipeline``. Here we assert the HTTP contract (03-api):
status codes, response shapes, and that domain errors map to the right codes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_capture_pipeline, require_session
from app.routers import capture
from app.services.capture_pipeline import (
    CaptureNotFound,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedAudio,
)
from app.services.capture_store import KIND_TEXT, RECEIVED, CaptureRecord

PREFIX = "/api/v1"


class FakeCapturePipeline:
    """Records delegations and replays canned results / errors for the router under test."""

    def __init__(self) -> None:
        self.records: dict[str, CaptureRecord] = {}
        self.text_calls: list[tuple[str, datetime | None]] = []
        self.voice_calls: list[tuple[bytes, str]] = []
        self.retried: list[str] = []
        self.follow_ups: list[tuple[str, str]] = []
        self.voice_error: Exception | None = None
        self.retry_error: Exception | None = None
        self.follow_up_error: Exception | None = None

    async def create_text_capture(self, text: str, *, created_at: datetime | None = None) -> str:
        self.text_calls.append((text, created_at))
        return "cid-text"

    async def create_voice_capture(self, audio: bytes, *, filename: str) -> str:
        if self.voice_error is not None:
            raise self.voice_error
        self.voice_calls.append((audio, filename))
        return "cid-voice"

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        return list(self.records.values())[:limit]

    async def get(self, capture_id: str) -> CaptureRecord | None:
        return self.records.get(capture_id)

    async def retry_capture(self, capture_id: str) -> None:
        if self.retry_error is not None:
            raise self.retry_error
        self.retried.append(capture_id)

    async def submit_follow_up(self, capture_id: str, answer: str) -> None:
        if self.follow_up_error is not None:
            raise self.follow_up_error
        self.follow_ups.append((capture_id, answer))


def _record(capture_id: str, **over) -> CaptureRecord:
    now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
    base = dict(
        id=capture_id,
        kind=KIND_TEXT,
        status=RECEIVED,
        raw_text="hi",
        node_paths=["memory/2026-07-12--a--018f0001.md"],
        created_at=now,
        updated_at=now,
    )
    base.update(over)
    return CaptureRecord(**base)


@pytest.fixture
def client_and_pipeline():
    app = FastAPI()
    app.include_router(capture.router, prefix=PREFIX)
    fake = FakeCapturePipeline()
    app.dependency_overrides[get_capture_pipeline] = lambda: fake
    app.dependency_overrides[require_session] = lambda: None  # bypass auth for these tests
    return TestClient(app), fake


def test_capture_text_returns_202(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(f"{PREFIX}/capture/text", json={"text": "a calm thought"})
    assert resp.status_code == 202
    assert resp.json() == {"capture_id": "cid-text", "status": "received"}
    assert fake.text_calls == [("a calm thought", None)]


def test_capture_text_forwards_created_at(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(
        f"{PREFIX}/capture/text",
        json={"text": "a synced note", "created_at": "2026-07-10T09:30:00+00:00"},
    )
    assert resp.status_code == 202
    text, created_at = fake.text_calls[0]
    assert text == "a synced note"
    assert created_at == datetime(2026, 7, 10, 9, 30, 0, tzinfo=UTC)


def test_capture_text_rejects_empty(client_and_pipeline):
    client, _ = client_and_pipeline
    resp = client.post(f"{PREFIX}/capture/text", json={"text": ""})
    assert resp.status_code == 422  # min_length=1


def test_capture_voice_returns_202(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(
        f"{PREFIX}/capture/voice",
        files={"file": ("memo.m4a", b"audio-bytes", "audio/m4a")},
    )
    assert resp.status_code == 202
    assert resp.json()["capture_id"] == "cid-voice"
    assert fake.voice_calls == [(b"audio-bytes", "memo.m4a")]


def test_capture_voice_unsupported_maps_to_400(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.voice_error = UnsupportedAudio("unsupported audio type: .txt")
    resp = client.post(
        f"{PREFIX}/capture/voice",
        files={"file": ("memo.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 400
    assert "unsupported audio" in resp.json()["detail"]


def test_list_captures_defaults_and_shape(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.records["a"] = _record("a", follow_up_question="how did that feel?")
    resp = client.get(f"{PREFIX}/captures")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["capture_id"] == "a"
    assert item["node_paths"] == ["memory/2026-07-12--a--018f0001.md"]
    assert item["follow_up_question"] == "how did that feel?"


def test_list_captures_rejects_out_of_range_limit(client_and_pipeline):
    client, _ = client_and_pipeline
    assert client.get(f"{PREFIX}/captures", params={"limit": 0}).status_code == 422
    assert client.get(f"{PREFIX}/captures", params={"limit": 101}).status_code == 422


def test_get_capture_found_and_missing(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.records["a"] = _record("a")
    assert client.get(f"{PREFIX}/captures/a").json()["capture_id"] == "a"
    assert client.get(f"{PREFIX}/captures/missing").status_code == 404


def test_retry_delegates_and_maps_errors(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(f"{PREFIX}/captures/a/retry")
    assert resp.status_code == 202
    assert fake.retried == ["a"]

    fake.retry_error = NotRetryable("a")
    assert client.post(f"{PREFIX}/captures/a/retry").status_code == 409

    fake.retry_error = CaptureNotFound("a")
    assert client.post(f"{PREFIX}/captures/a/retry").status_code == 404


def test_follow_up_delegates_and_maps_errors(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(f"{PREFIX}/captures/a/follow-up", json={"answer": "more detail"})
    assert resp.status_code == 202
    assert fake.follow_ups == [("a", "more detail")]

    fake.follow_up_error = FollowUpNotPending("a")
    r = client.post(f"{PREFIX}/captures/a/follow-up", json={"answer": "x"})
    assert r.status_code == 409

    fake.follow_up_error = CaptureNotFound("a")
    r = client.post(f"{PREFIX}/captures/a/follow-up", json={"answer": "x"})
    assert r.status_code == 404


def test_follow_up_rejects_empty_answer(client_and_pipeline):
    client, _ = client_and_pipeline
    resp = client.post(f"{PREFIX}/captures/a/follow-up", json={"answer": ""})
    assert resp.status_code == 422


def test_capture_endpoints_require_session():
    # No require_session override → the router-level gate must reject an unauthenticated request.
    app = FastAPI()
    app.state.settings = Settings(session_cookie_name="braindan_session")

    class _DenyAuth:
        async def validate(self, token):
            return None

    app.state.auth_service = _DenyAuth()
    app.include_router(capture.router, prefix=PREFIX)
    client = TestClient(app)
    assert client.get(f"{PREFIX}/captures").status_code == 401
    assert client.post(f"{PREFIX}/capture/text", json={"text": "hi"}).status_code == 401
