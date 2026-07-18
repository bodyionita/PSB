"""Media-derivation stage tests (M9 T2, ADR-057 §3/§5): photo→vision, voice→STT, bounded retries →
`unavailable`, idempotent skip, and targeted re-derive. Fakes only — no live LLM/DB/network."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.registry import ProviderRegistry
from app.services.agent_runs import SUCCEEDED
from app.services.media_derivation import (
    MEDIA_DESCRIPTION_SYSTEM_PROMPT,
    MediaDerivationService,
    placeholder,
)
from app.services.media_store import DERIVED, PENDING, UNAVAILABLE, MediaFiles

from .fakes import (
    FakeAgentRunStore,
    FakeChatProvider,
    FakeMediaStore,
    FakeSTTProvider,
    fake_routing,
)

VLM = "vlm"
STT = "stt"
PNG_BYTES = b"\x89PNG fake bytes"


def _service(tmp_path, *, vlm=None, stt=None, max_attempts=3):
    vlm = vlm or FakeChatProvider(VLM, reply="a photo of a cat sitting on a laptop")
    stt = stt or FakeSTTProvider(STT, transcript="hello from the voice note")
    registry = ProviderRegistry(
        {VLM: vlm, STT: stt},
        chat_chain=[VLM],
        distill_chain=[VLM],
        embedding_provider_id="none",
        stt_chain=[STT],
    )
    routing = fake_routing(registry, chain=(VLM,))
    store = FakeMediaStore()
    files = MediaFiles(Settings(data_path=str(tmp_path)))
    runs = FakeAgentRunStore()
    service = MediaDerivationService(
        store=store,
        files=files,
        routing=routing,
        registry=registry,
        run_store=runs,
        max_attempts=max_attempts,
        rederive_max_per_run=50,
    )
    return service, store, files, runs, vlm, stt


async def _photo(store, files, *, mime="image/png", name="shot.png"):
    rel = files.relative_path("captures", name)
    await files.write_async(rel, PNG_BYTES)
    return await store.create(kind="photo", source="capture", file_path=rel, mime_type=mime)


# --- happy path ---------------------------------------------------------------------------------


async def test_photo_derives_via_vision_group(tmp_path):
    service, store, files, _runs, vlm, _stt = _service(tmp_path)
    media = await _photo(store, files)

    outcome = await service.derive_one(media.id)

    assert outcome.status == DERIVED
    row = await store.get(media.id)
    assert row.status == DERIVED
    assert row.derived_text == "a photo of a cat sitting on a laptop"
    assert row.model_used == VLM
    assert row.error is None
    # The image reached the VLM as a data URI (the ADR-057 §4 seam), behind the §5 contract prompt.
    assert vlm.images_seen[-1] == [f"data:image/png;base64,{_b64(PNG_BYTES)}"]
    assert vlm.last_messages[0].content == MEDIA_DESCRIPTION_SYSTEM_PROMPT


async def test_voice_derives_via_stt_chain(tmp_path):
    service, store, files, _runs, _vlm, stt = _service(tmp_path)
    rel = files.relative_path("captures", "note.m4a")
    await files.write_async(rel, b"fake audio")
    media = await store.create(kind="voice", source="capture", file_path=rel, mime_type="audio/mp4")

    outcome = await service.derive_one(media.id)

    assert outcome.status == DERIVED
    assert (await store.get(media.id)).derived_text == "hello from the voice note"
    assert stt.calls == 1


async def test_derive_is_idempotent_on_already_derived(tmp_path):
    service, store, files, _runs, vlm, _stt = _service(tmp_path)
    media = await _photo(store, files)
    await service.derive_one(media.id)
    calls_after_first = vlm.calls

    outcome = await service.derive_one(media.id)  # second pass

    assert outcome.status == "skipped"
    assert vlm.calls == calls_after_first  # no second model call


# --- bounded retries → unavailable --------------------------------------------------------------


async def test_failed_attempt_stays_pending_then_goes_unavailable(tmp_path):
    down = FakeChatProvider(VLM, available=False)
    service, store, files, _runs, _vlm, _stt = _service(tmp_path, vlm=down, max_attempts=2)
    media = await _photo(store, files)

    first = await service.derive_one(media.id)
    assert first.status == "pending"  # retry left
    assert (await store.get(media.id)).status == PENDING
    assert (await store.get(media.id)).attempts == 1

    second = await service.derive_one(media.id)
    assert second.status == UNAVAILABLE
    row = await store.get(media.id)
    assert row.status == UNAVAILABLE
    assert row.attempts == 2
    assert row.error  # the failure reason is recorded (rule 7)


async def test_empty_derivation_is_treated_as_failure(tmp_path):
    blank = FakeChatProvider(VLM, reply="   ")
    service, store, files, _runs, _vlm, _stt = _service(tmp_path, vlm=blank, max_attempts=1)
    media = await _photo(store, files)

    outcome = await service.derive_one(media.id)

    assert outcome.status == UNAVAILABLE  # empty text never counts as a description


async def test_missing_media_is_skipped(tmp_path):
    service, *_ = _service(tmp_path)
    outcome = await service.derive_one("00000000-0000-0000-0000-000000000123")
    assert outcome.status == "skipped"


# --- targeted re-derive -------------------------------------------------------------------------


async def test_rederive_recovers_unavailable_items(tmp_path):
    # First run with a down VLM drives the item to unavailable; a healthy re-derive recovers it.
    down = FakeChatProvider(VLM, available=False)
    service, store, files, runs, _vlm, _stt = _service(tmp_path, vlm=down, max_attempts=1)
    media = await _photo(store, files)
    await service.derive_one(media.id)
    assert (await store.get(media.id)).status == UNAVAILABLE

    # Swap in a healthy VLM and re-derive the unavailable backlog.
    service._routing = fake_routing(
        ProviderRegistry(
            {VLM: FakeChatProvider(VLM, reply="recovered description")},
            chat_chain=[VLM],
            distill_chain=[VLM],
            embedding_provider_id="none",
            stt_chain=[],
        ),
        chain=(VLM,),
    )
    result = await service.rederive()

    assert (result.considered, result.derived, result.unavailable) == (1, 1, 0)
    row = await store.get(media.id)
    assert row.status == DERIVED
    assert row.attempts == 1  # reset to 0 by re-derive, then one successful attempt
    assert row.derived_text == "recovered description"
    # The re-derive opened + closed its own agent_runs row (vision P8).
    run = await runs.latest("media-rederive")
    assert run is not None and run.status == SUCCEEDED


async def test_rederive_explicit_ids_only(tmp_path):
    service, store, files, _runs, _vlm, _stt = _service(tmp_path)
    a = await _photo(store, files)
    b = await _photo(store, files)

    result = await service.rederive(media_ids=[a.id])

    assert result.considered == 1
    assert (await store.get(a.id)).status == DERIVED
    assert (await store.get(b.id)).status == PENDING  # untouched


def test_placeholder_texts_are_explicit():
    assert placeholder("photo") == "<photo — description unavailable>"
    assert placeholder("voice") == "<voice note — transcript unavailable>"


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
