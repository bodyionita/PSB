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
    DraftNotOpen,
    EmptyDraft,
    FollowUpNotPending,
    NotRetryable,
    UnsupportedImage,
    VoicePartLimit,
)
from app.services.capture_store import (
    DRAFT,
    KIND_COMPOSITE,
    KIND_TEXT,
    RECEIVED,
    CaptureMediaRef,
    CaptureNodeRef,
    CaptureRecord,
)
from app.services.media_store import MediaRecord

PREFIX = "/api/v1"


class FakeCapturePipeline:
    """Records delegations and replays canned results / errors for the router under test."""

    def __init__(self) -> None:
        self.records: dict[str, CaptureRecord] = {}
        self.text_calls: list[tuple[str, datetime | None]] = []
        self.voice_calls: list[tuple[bytes, str]] = []
        self.image_calls: list[tuple[bytes, str]] = []
        self.retried: list[str] = []
        self.follow_ups: list[tuple[str, str]] = []
        self.anchor_edits: list[tuple[str, datetime]] = []
        self.anchor_error: Exception | None = None
        self.voice_error: Exception | None = None
        self.image_error: Exception | None = None
        self.retry_error: Exception | None = None
        self.follow_up_error: Exception | None = None
        # composite draft lifecycle (M9.6 T1)
        self.draft_part_rows: list[MediaRecord] = []
        self.part_calls: list[tuple[str, str, str]] = []
        self.removed_parts: list[tuple[str, str]] = []
        self.draft_texts: list[tuple[str, str]] = []
        self.submitted: list[str] = []
        self.discarded: list[str] = []
        self.part_error: Exception | None = None
        self.draft_text_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.discard_error: Exception | None = None

    async def create_text_capture(self, text: str, *, created_at: datetime | None = None) -> str:
        self.text_calls.append((text, created_at))
        return "cid-text"

    async def create_voice_capture(self, audio: bytes, *, filename: str) -> str:
        if self.voice_error is not None:
            raise self.voice_error
        self.voice_calls.append((audio, filename))
        return "cid-voice"

    async def create_image_capture(self, image: bytes, *, filename: str) -> str:
        if self.image_error is not None:
            raise self.image_error
        self.image_calls.append((image, filename))
        return "cid-image"

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

    async def edit_anchor(self, capture_id: str, new_anchor: datetime) -> None:
        if self.anchor_error is not None:
            raise self.anchor_error
        self.anchor_edits.append((capture_id, new_anchor))

    # --- composite draft lifecycle (M9.6 T1) ---
    async def open_or_resume_draft(self) -> CaptureRecord:
        rec = CaptureRecord(id="cid-draft", kind=KIND_COMPOSITE, status=DRAFT, source="web")
        self.records[rec.id] = rec
        return rec

    async def draft_parts(self, capture_id: str) -> list[MediaRecord]:
        return list(self.draft_part_rows)

    async def add_draft_part(
        self, capture_id: str, data: bytes, *, filename: str, kind: str
    ) -> MediaRecord:
        if self.part_error is not None:
            raise self.part_error
        media = MediaRecord(
            id="media-1", kind=kind, source="capture", status="pending", part_ordinal=0
        )
        self.part_calls.append((capture_id, filename, kind))
        return media

    async def remove_draft_part(self, capture_id: str, media_id: str) -> None:
        if self.part_error is not None:
            raise self.part_error
        self.removed_parts.append((capture_id, media_id))

    async def set_draft_text(self, capture_id: str, text: str) -> None:
        if self.draft_text_error is not None:
            raise self.draft_text_error
        self.draft_texts.append((capture_id, text))

    async def submit_draft(self, capture_id: str) -> None:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(capture_id)

    async def discard_draft(self, capture_id: str) -> None:
        if self.discard_error is not None:
            raise self.discard_error
        self.discarded.append(capture_id)


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


# The three one-shot `POST /capture/{text,voice,image}` endpoints are removed in M9.6 (ADR-061 §8);
# every web capture goes through the composite draft flow (covered below + in test_composite_draft).


def test_capture_view_carries_media_list(client_and_pipeline):
    # M9.6 T4 (ADR-061 §11): CaptureView carries media as an ordered LIST + text_body
    # + the Activity-run deep-link, so the web renders each part (GET /media/{id}) + a status badge.
    client, fake = client_and_pipeline
    fake.records["cmp"] = _record(
        "cmp",
        kind="composite",
        text_body="my caption",
        media_refs=[
            CaptureMediaRef(id="media-1", kind="photo", status="derived", part_ordinal=0),
            CaptureMediaRef(id="media-2", kind="voice", status="pending", part_ordinal=1),
        ],
        run_id="run-9",
    )
    body = client.get(f"{PREFIX}/captures/cmp").json()
    assert body["text_body"] == "my caption"
    assert body["run_id"] == "run-9"
    assert body["media"] == [
        {"id": "media-1", "kind": "photo", "status": "derived", "part_ordinal": 0},
        {"id": "media-2", "kind": "voice", "status": "pending", "part_ordinal": 1},
    ]


def test_capture_view_media_default_empty(client_and_pipeline):
    # A text capture has no media parts — the field is an empty list, never an error.
    client, fake = client_and_pipeline
    fake.records["a"] = _record("a")
    assert client.get(f"{PREFIX}/captures/a").json()["media"] == []


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


def test_get_capture_carries_node_refs(client_and_pipeline):
    # M8.1 T4 (ADR-054 §5 replan): CaptureView.node_refs is the id-resolved projection of
    # node_paths — the field the web NodeChip needs (GET /nodes/{id} is uuid-keyed, paths aren't
    # identity). A path with no resolved ref (not yet indexed / tombstoned) is simply absent.
    client, fake = client_and_pipeline
    fake.records["a"] = _record(
        "a",
        node_paths=["memory/2026-07-12--a--018f0001.md", "inbox/2026-07-12--b--018f0002.md"],
        node_refs=[
            CaptureNodeRef(
                id="018f0001-0000-0000-0000-000000000001",
                store_path="memory/2026-07-12--a--018f0001.md",
                type="memory",
                title="A calm thought",
            )
        ],
    )
    body = client.get(f"{PREFIX}/captures/a").json()
    assert body["node_paths"] == [
        "memory/2026-07-12--a--018f0001.md",
        "inbox/2026-07-12--b--018f0002.md",
    ]
    assert body["node_refs"] == [
        {
            "id": "018f0001-0000-0000-0000-000000000001",
            "store_path": "memory/2026-07-12--a--018f0001.md",
            "type": "memory",
            "title": "A calm thought",
        }
    ]


def test_list_captures_node_refs_default_empty(client_and_pipeline):
    # A capture with no node_refs (freshly received, or the fake's default) round-trips an empty
    # list, never null — the web renders `node_paths` plain in that case.
    client, fake = client_and_pipeline
    fake.records["a"] = _record("a")
    body = client.get(f"{PREFIX}/captures").json()
    assert body[0]["node_refs"] == []


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


def test_anchor_edit_delegates_and_maps_errors(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.put(f"{PREFIX}/captures/a/anchor", json={"anchor": "2026-07-07T08:40:00+03:00"})
    assert resp.status_code == 202
    assert len(fake.anchor_edits) == 1 and fake.anchor_edits[0][0] == "a"

    fake.anchor_error = CaptureNotFound("a")
    r = client.put(f"{PREFIX}/captures/a/anchor", json={"anchor": "2026-07-07T08:40:00+03:00"})
    assert r.status_code == 404


def test_anchor_edit_rejects_missing_anchor(client_and_pipeline):
    client, _ = client_and_pipeline
    resp = client.put(f"{PREFIX}/captures/a/anchor", json={})
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
    assert client.post(f"{PREFIX}/capture/draft").status_code == 401


# --- Composite draft lifecycle (M9.6 T1, ADR-061 §3) ---
def test_open_draft_returns_draft_view(client_and_pipeline):
    client, _ = client_and_pipeline
    resp = client.post(f"{PREFIX}/capture/draft")
    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_id"] == "cid-draft"
    assert body["status"] == "draft"
    assert body["parts"] == []


def test_add_part_returns_part_view(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(
        f"{PREFIX}/capture/cid-draft/part",
        data={"kind": "photo"},
        files={"file": ("p.png", b"img", "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "photo"
    assert fake.part_calls == [("cid-draft", "p.png", "photo")]


def test_add_part_voice_limit_maps_to_409(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.part_error = VoicePartLimit("cid-draft")
    resp = client.post(
        f"{PREFIX}/capture/cid-draft/part",
        data={"kind": "voice"},
        files={"file": ("v.m4a", b"a", "audio/m4a")},
    )
    assert resp.status_code == 409


def test_add_part_bad_type_maps_to_400(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.part_error = UnsupportedImage("nope")
    resp = client.post(
        f"{PREFIX}/capture/cid-draft/part",
        data={"kind": "photo"},
        files={"file": ("p.gif", b"a", "image/gif")},
    )
    assert resp.status_code == 400


def test_remove_part_returns_204(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.delete(f"{PREFIX}/capture/cid-draft/part/media-1")
    assert resp.status_code == 204
    assert fake.removed_parts == [("cid-draft", "media-1")]


def test_remove_part_not_draft_maps_to_409(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.part_error = DraftNotOpen("cid-draft")
    resp = client.delete(f"{PREFIX}/capture/cid-draft/part/media-1")
    assert resp.status_code == 409


def test_edit_text_returns_draft_view(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.records["cid-draft"] = CaptureRecord(
        id="cid-draft", kind=KIND_COMPOSITE, status=DRAFT, text_body="hi", source="web"
    )
    resp = client.put(f"{PREFIX}/capture/cid-draft/text", json={"text": "hi"})
    assert resp.status_code == 200
    assert fake.draft_texts == [("cid-draft", "hi")]


def test_submit_returns_202(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.post(f"{PREFIX}/capture/cid-draft/submit")
    assert resp.status_code == 202
    assert fake.submitted == ["cid-draft"]


def test_submit_empty_maps_to_400(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.submit_error = EmptyDraft("cid-draft")
    resp = client.post(f"{PREFIX}/capture/cid-draft/submit")
    assert resp.status_code == 400


def test_submit_non_draft_maps_to_409(client_and_pipeline):
    client, fake = client_and_pipeline
    fake.submit_error = DraftNotOpen("cid-draft")
    resp = client.post(f"{PREFIX}/capture/cid-draft/submit")
    assert resp.status_code == 409


def test_discard_returns_204(client_and_pipeline):
    client, fake = client_and_pipeline
    resp = client.delete(f"{PREFIX}/capture/cid-draft/draft")
    assert resp.status_code == 204
    assert fake.discarded == ["cid-draft"]
