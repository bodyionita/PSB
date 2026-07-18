"""Legacy-voice → media backfill (ADR-060 §5) — the idempotent, degrading deploy-time op.

Voice audio predates the media substrate: pre-ADR-060 captures saved the audio under ``DATA_PATH``
(``captures.audio_path``) with no ``media`` row, so it was never servable, its transcript never
re-derivable, and no ``node_media`` link put it on the node. This one-shot op heals that:

    for each legacy voice capture (audio_path set, no media row, not removed):
      relocate the audio into the media layout  →  mint a `voice` media row (derived_text = the
      existing transcript)  →  rebuild its node↔media link against the capture's content nodes.

**Idempotent** (rule 6): the scan selects only captures with **no** media row, so a second run is a
no-op — an already-backfilled capture is skipped. **Degrading** (rule 2/7): a missing legacy audio
file never fails the op — it mints a media row that degrades (``unavailable`` when there is no
transcript either, ``derived`` when the transcript survives) so the node stays visible + linked,
just not playable. Runs under its own ``agent_runs`` row (vision P8). Never re-runs STT — the
existing transcript is the derived text (parity with reprocess replaying ``raw_text``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..db import Database
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore
from .media_store import (
    DERIVED,
    KIND_VOICE,
    PENDING,
    SOURCE_CAPTURE,
    UNAVAILABLE,
    MediaFiles,
    MediaStore,
)
from .node_media_store import NodeMediaStore

logger = logging.getLogger(__name__)

# agent_runs.agent name for this op (visible in the activity feed, vision P8).
AGENT = "voice-media-backfill"

# Container ext → mime for the relocated voice row, so `GET /media/{id}` streams the right header
# (mirrors the capture pipeline's `_AUDIO_MIME`; kept local so the backfill has no import into the
# pipeline module). An unmapped ext leaves `mime_type` NULL (served as octet-stream).
_AUDIO_MIME = {
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


@dataclass(frozen=True)
class LegacyVoiceCapture:
    """A pre-ADR-060 voice capture awaiting backfill: its stored legacy audio path + transcript +
    the content nodes its media should re-attach to."""

    capture_id: str
    audio_path: str
    transcript: str | None
    node_paths: list[str]


@dataclass(frozen=True)
class BackfillOutcome:
    """What one backfill pass touched (aggregated into its ``agent_runs`` summary)."""

    considered: int = 0
    relocated: int = 0  # audio found + moved into the media layout
    degraded: int = 0  # audio missing → media row minted without a servable file
    linked: int = 0  # captures whose media got a node_media link


class VoiceBackfillStore(Protocol):
    """The reads the op needs (plain SQL, ADR-011)."""

    async def legacy_voice_captures(self) -> list[LegacyVoiceCapture]:
        """Voice captures with a legacy ``audio_path``, **no** ``media`` row, and not
        one-tap-removed — the backfill set (the no-media filter makes the op idempotent)."""
        ...

    async def content_node_ids(self, paths: list[str], *, entity_types: list[str]) -> list[str]:
        """The ids of a capture's **content** nodes among ``paths`` (ADR-060 §2: media links to
        content nodes, never to minted entity hubs) — live (non-tombstoned) ``nodes`` whose type is
        not an entity-hub type."""
        ...


class VoiceMediaBackfillService:
    """Relocate legacy voice audio → mint media rows → rebuild the node↔media links (ADR-060 §5)."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: VoiceBackfillStore,
        media_store: MediaStore,
        media_files: MediaFiles,
        node_media_store: NodeMediaStore,
        run_store: AgentRunStore,
        entity_types: list[str],
    ) -> None:
        self._settings = settings
        self._store = store
        self._media_store = media_store
        self._media_files = media_files
        self._node_media = node_media_store
        self._runs = run_store
        self._entity_types = list(entity_types)
        self._data_root = Path(settings.data_path)

    async def run(self) -> BackfillOutcome:
        """Back-fill every legacy voice capture once. Best-effort per capture (rule 7): one bad item
        is logged + counted, never aborting the pass. Own ``agent_runs`` row (P8)."""
        run_id = await self._start_run()
        try:
            legacy = await self._store.legacy_voice_captures()
            relocated = degraded = linked = 0
            for item in legacy:
                try:
                    result = await self._backfill_one(item)
                except Exception:  # noqa: BLE001 — one bad capture must not abort the backfill
                    logger.exception("voice backfill of %s failed (skipped)", item.capture_id)
                    continue
                relocated += 1 if result.relocated else 0
                degraded += 0 if result.relocated else 1
                linked += 1 if result.linked else 0
            outcome = BackfillOutcome(
                considered=len(legacy), relocated=relocated, degraded=degraded, linked=linked
            )
            await self._finish_run(run_id, outcome)
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("voice-media-backfill failed")
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}")
            raise

    @dataclass(frozen=True)
    class _OneResult:
        relocated: bool
        linked: bool

    async def _backfill_one(self, item: LegacyVoiceCapture) -> _OneResult:
        rel_path = self._media_files.relative_path(SOURCE_CAPTURE, _basename(item.audio_path))
        legacy_abs = self._data_root / item.audio_path
        has_transcript = bool((item.transcript or "").strip())
        mime = _AUDIO_MIME.get(_ext(item.audio_path))

        if await asyncio.to_thread(legacy_abs.is_file):
            # Relocate (copy — the legacy file is left in place; deleting raw is not this op's job).
            data = await asyncio.to_thread(legacy_abs.read_bytes)
            await self._media_files.write_async(rel_path, data)
            media = await self._media_store.create(
                kind=KIND_VOICE,
                source=SOURCE_CAPTURE,
                capture_id=item.capture_id,
                file_path=rel_path,
                mime_type=mime,
                status=DERIVED if has_transcript else PENDING,
                derived_text=item.transcript if has_transcript else None,
            )
            relocated = True
        else:
            # Missing audio degrades (ADR-060 §5): mint a fileless row so the node stays visible +
            # linked. `derived` if the transcript survives (still readable), else `unavailable`.
            logger.warning(
                "voice backfill: legacy audio %s for capture %s is gone; degrading",
                item.audio_path,
                item.capture_id,
            )
            media = await self._media_store.create(
                kind=KIND_VOICE,
                source=SOURCE_CAPTURE,
                capture_id=item.capture_id,
                mime_type=mime,
                status=DERIVED if has_transcript else UNAVAILABLE,
                derived_text=item.transcript if has_transcript else None,
            )
            relocated = False

        content_ids = await self._store.content_node_ids(
            item.node_paths, entity_types=self._entity_types
        )
        await self._node_media.rebuild_for_media(media_ids=[media.id], node_ids=content_ids)
        return self._OneResult(relocated=relocated, linked=bool(content_ids))

    # --- agent_runs plumbing (best-effort; never breaks the op) --------------------------------

    async def _start_run(self) -> str | None:
        try:
            return await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — logging must not break the op (rule 7)
            logger.exception("could not open agent_runs row for voice backfill (logging degraded)")
            return None

    async def _finish_run(self, run_id: str | None, outcome: BackfillOutcome) -> None:
        if run_id is None:
            return
        summary = (
            f"voice-media-backfill: {outcome.considered} legacy voice capture(s) — "
            f"{outcome.relocated} relocated, {outcome.degraded} degraded (audio missing), "
            f"{outcome.linked} node-linked"
        )
        try:
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=summary, details=outcome.__dict__
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not close voice-backfill run %s (logging degraded)", run_id)

    async def _fail_run(self, run_id: str | None, error: str) -> None:
        if run_id is None:
            return
        try:
            await self._runs.finish(run_id, status=FAILED, error=error)
        except Exception:  # noqa: BLE001
            logger.exception("could not close voice-backfill run %s (logging degraded)", run_id)


class PgVoiceBackfillStore:
    """asyncpg-backed backfill reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def legacy_voice_captures(self) -> list[LegacyVoiceCapture]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, audio_path, raw_text, node_paths
                  FROM captures c
                 WHERE c.kind = 'voice'
                   AND c.audio_path IS NOT NULL
                   AND c.removed_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM media m WHERE m.capture_id = c.id)
                 ORDER BY c.created_at ASC, c.id
                """
            )
        return [
            LegacyVoiceCapture(
                capture_id=str(r["id"]),
                audio_path=r["audio_path"],
                transcript=r["raw_text"],
                node_paths=list(r["node_paths"] or []),
            )
            for r in rows
        ]

    async def content_node_ids(self, paths: list[str], *, entity_types: list[str]) -> list[str]:
        if not paths:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM nodes
                 WHERE store_path = ANY($1::text[])
                   AND merged_into IS NULL
                   AND NOT (type = ANY($2::text[]))
                """,
                paths,
                entity_types,
            )
        return [str(r["id"]) for r in rows]


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    name = _basename(path)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


async def build_voice_media_backfill_service(
    settings: Settings, db: Database
) -> VoiceMediaBackfillService:
    """Construct a standalone backfill service for the CLI verb (``python -m app.cli
    voice-media-backfill``) — the deploy-time op T6 runs after migration 018. Async because it
    resolves the **effective** entity vocabulary (seeds ∪ approved additions) so the content-node
    filter matches the organizer (ADR-027 forward-live)."""
    from ..vocab.service import VocabularyService, effective_vocabulary
    from ..vocab.store import PgVocabularyStore
    from .agent_runs import PgAgentRunStore
    from .media_store import PgMediaStore
    from .node_media_store import PgNodeMediaStore

    vocab = VocabularyService(settings=settings, vocab_store=PgVocabularyStore(db))
    entity_types = list((await effective_vocabulary(vocab, settings)).entity_like_types)
    return VoiceMediaBackfillService(
        settings=settings,
        store=PgVoiceBackfillStore(db),
        media_store=PgMediaStore(db),
        media_files=MediaFiles(settings),
        node_media_store=PgNodeMediaStore(db),
        run_store=PgAgentRunStore(db),
        entity_types=entity_types,
    )
