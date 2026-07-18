"""Voice-media backfill tests (ADR-060 §5): relocate legacy voice audio → mint media rows → link
node_media. Fakes only (no DB): a preset backfill store + FakeMediaStore + a real MediaFiles over a
tmp volume + FakeNodeMediaStore. Covers the relocate + degrade + link + idempotency contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services.media_backfill import (
    LegacyVoiceCapture,
    VoiceMediaBackfillService,
)
from app.services.media_store import DERIVED, UNAVAILABLE, MediaFiles

from .fakes import FakeAgentRunStore, FakeMediaStore, FakeNodeMediaStore

pytestmark = pytest.mark.asyncio


class FakeVoiceBackfillStore:
    """Preset legacy-voice roster + a content-node resolver (mirrors PgVoiceBackfillStore)."""

    def __init__(
        self,
        *,
        legacy: list[LegacyVoiceCapture],
        content_ids: dict[str, list[str]] | None = None,
    ) -> None:
        self._legacy = list(legacy)
        self._content_ids = dict(content_ids or {})
        self.content_calls: list[list[str]] = []

    async def legacy_voice_captures(self) -> list[LegacyVoiceCapture]:
        return list(self._legacy)

    async def content_node_ids(self, paths, *, entity_types):
        self.content_calls.append(list(paths))
        # A tiny stand-in for the SQL: map each path to a preset content node id, skipping any the
        # test flagged as an entity hub (absent from the map).
        return [self._content_ids[p] for p in paths if p in self._content_ids]


def _service(tmp_path: Path, store: FakeVoiceBackfillStore):
    settings = Settings(data_path=str(tmp_path / "data"), scheduler_tz="UTC")
    media_store = FakeMediaStore()
    media_files = MediaFiles(settings)
    node_media = FakeNodeMediaStore()
    service = VoiceMediaBackfillService(
        settings=settings,
        store=store,
        media_store=media_store,
        media_files=media_files,
        node_media_store=node_media,
        run_store=FakeAgentRunStore(),
        entity_types=["person"],
    )
    return service, media_store, media_files, node_media, settings


async def test_backfill_relocates_audio_mints_media_and_links(tmp_path: Path):
    # A legacy voice capture: audio under DATA_PATH, a stored transcript, one content node.
    legacy = LegacyVoiceCapture(
        capture_id="cap-1",
        audio_path="voice-1.m4a",
        transcript="a spoken memo",
        node_paths=["memory/2026-01-01--memo--abcd.md"],
    )
    store = FakeVoiceBackfillStore(
        legacy=[legacy], content_ids={"memory/2026-01-01--memo--abcd.md": "node-1"}
    )
    service, media_store, media_files, node_media, settings = _service(tmp_path, store)
    # Seed the legacy audio on disk under DATA_PATH.
    Path(settings.data_path).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — trivial test IO
    (Path(settings.data_path) / "voice-1.m4a").write_bytes(b"legacy-audio")

    outcome = await service.run()

    assert outcome.considered == 1 and outcome.relocated == 1 and outcome.linked == 1
    media = await media_store.get_by_capture_id("cap-1")
    assert media.kind == "voice" and media.source == "capture"
    assert media.status == DERIVED and media.derived_text == "a spoken memo"
    assert media.file_path == "capture/voice-1.m4a" and media.mime_type == "audio/mp4"
    # Audio relocated into the media layout, streamable via GET /media/{id}.
    assert media_files.absolute(media.file_path).read_bytes() == b"legacy-audio"
    # The content node is linked to the media (ADR-060 §1).
    assert ("node-1", media.id) in node_media.links


async def test_backfill_degrades_when_audio_missing(tmp_path: Path):
    # The legacy audio file is gone: the op degrades — it mints a fileless media row (still
    # `derived` because the transcript survives), never failing the pass (ADR-060 §5).
    legacy = LegacyVoiceCapture(
        capture_id="cap-2",
        audio_path="gone.m4a",
        transcript="surviving transcript",
        node_paths=["memory/x--1.md"],
    )
    store = FakeVoiceBackfillStore(legacy=[legacy], content_ids={"memory/x--1.md": "node-2"})
    service, media_store, _files, node_media, _s = _service(tmp_path, store)

    outcome = await service.run()

    assert outcome.considered == 1 and outcome.relocated == 0 and outcome.degraded == 1
    media = await media_store.get_by_capture_id("cap-2")
    assert media.file_path is None and media.status == DERIVED  # transcript kept it readable
    assert ("node-2", media.id) in node_media.links


async def test_backfill_degrades_to_unavailable_without_transcript(tmp_path: Path):
    # No audio AND no transcript → the item is `unavailable` (a visible, linked, but empty stub).
    legacy = LegacyVoiceCapture(
        capture_id="cap-3", audio_path="gone.m4a", transcript=None, node_paths=[]
    )
    store = FakeVoiceBackfillStore(legacy=[legacy])
    service, media_store, _files, _nm, _s = _service(tmp_path, store)

    await service.run()

    media = await media_store.get_by_capture_id("cap-3")
    assert media.status == UNAVAILABLE and media.file_path is None


async def test_backfill_is_idempotent_via_store_filter(tmp_path: Path):
    # Idempotency is the store's job (it selects only captures with NO media row). Once a capture is
    # backfilled the roster no longer returns it, so a second run is a no-op — assert one mint.
    legacy = LegacyVoiceCapture(
        capture_id="cap-4", audio_path="v.m4a", transcript="t", node_paths=[]
    )
    store = FakeVoiceBackfillStore(legacy=[legacy])
    service, media_store, _files, _nm, settings = _service(tmp_path, store)
    Path(settings.data_path).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — trivial test IO
    (Path(settings.data_path) / "v.m4a").write_bytes(b"a")

    await service.run()
    # Simulate the store's no-media filter: the capture is now backfilled, so it leaves the roster.
    store._legacy = []
    outcome = await service.run()

    assert outcome.considered == 0
    assert len([m for m in media_store.rows.values() if m.capture_id == "cap-4"]) == 1
