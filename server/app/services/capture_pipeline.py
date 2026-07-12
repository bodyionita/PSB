"""Capture pipeline (04-pipelines §1, ADR-019).

Orchestrates a capture from raw input to vault notes, in-process via ``asyncio.create_task``
(no broker — M1 build decisions). The public methods return immediately after the raw input is
persisted; the heavy work (transcribe → organize → write → index-stub → trailing nudge) runs in
the background so the API can answer ``202`` and the note lands well under the <30s criterion.

Invariants honoured here:
  * **Never lose input** (rule 2): the ``captures`` row — and, for voice, the audio file under
    ``DATA_PATH`` — is persisted *before* any model call. Model failures degrade to an Inbox
    note; only infrastructure failures (STT, vault write) mark a capture ``failed``.
  * **Everything visible / no crash** (rule 7): every background task is wrapped; failures end
    as ``status=failed`` with context, never an unhandled task exception.
  * **Async end-to-end** (rule 8): filesystem work goes through ``asyncio.to_thread``.
  * **Boot-time sweep**: interrupted in-flight captures are marked ``failed`` (retryable).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..capture.notes import NoteWriter
from ..capture.organizer import (
    NUDGE_SYSTEM_PROMPT,
    ORGANIZER_SYSTEM_PROMPT,
    OrganizeResult,
    OrganizerNote,
    inbox_fallback_note,
    parse_organizer_json,
    validate_organizer_output,
)
from ..config import Settings
from ..providers.base import ChatMessage, ProviderUnavailable
from ..providers.registry import ProviderRegistry
from .capture_store import (
    INDEXED,
    KIND_TEXT,
    KIND_VOICE,
    ORGANIZING,
    RECEIVED,
    TRANSCRIBING,
    WRITTEN,
    CaptureStore,
)
from .vault_backup import VaultBackup

logger = logging.getLogger(__name__)

# Audio container extensions accepted by POST /capture/voice (03-api.md).
ALLOWED_AUDIO_EXTS = frozenset({"m4a", "webm", "ogg", "mp3", "wav"})
_ORPHAN_ERROR = "interrupted by restart"
_MAX_NUDGE_CHARS = 300  # a one-line question; guards against a runaway model reply


class CaptureError(Exception):
    """Base for capture problems surfaced to the API layer."""


class UnsupportedAudio(CaptureError):
    """The uploaded audio is too large or an unsupported container."""


class CaptureNotFound(CaptureError):
    """No capture with the given id."""


class FollowUpNotPending(CaptureError):
    """The capture has no pending follow-up question to answer (409)."""


class CapturePipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        store: CaptureStore,
        registry: ProviderRegistry,
        note_writer: NoteWriter,
        vault_backup: VaultBackup,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._notes = note_writer
        self._backup = vault_backup
        self._tz = ZoneInfo(settings.scheduler_tz)
        # Strong refs to in-flight background tasks so they are not GC'd mid-run.
        self._tasks: set[asyncio.Task] = set()

    # --- public API ---------------------------------------------------------------------

    async def create_text_capture(self, text: str, *, created_at: datetime | None = None) -> str:
        capture_id = str(uuid.uuid4())
        await self._store.create(
            capture_id=capture_id,
            kind=KIND_TEXT,
            status=RECEIVED,
            raw_text=text,
            created_at=created_at,
        )
        self._spawn(self._process(capture_id))
        return capture_id

    async def create_voice_capture(self, audio: bytes, *, filename: str) -> str:
        if len(audio) > self._settings.audio_max_bytes:
            raise UnsupportedAudio(
                f"audio exceeds {self._settings.audio_max_bytes} bytes (Whisper limit)"
            )
        ext = _audio_ext(filename)
        if ext not in ALLOWED_AUDIO_EXTS:
            raise UnsupportedAudio(f"unsupported audio type: .{ext}")

        capture_id = str(uuid.uuid4())
        stored_name = f"{capture_id}.{ext}"
        # Persist the audio to disk BEFORE the row exists / any model call — never-lose.
        await asyncio.to_thread(self._write_audio, stored_name, audio)
        await self._store.create(
            capture_id=capture_id,
            kind=KIND_VOICE,
            status=RECEIVED,
            audio_path=stored_name,
        )
        self._spawn(self._process(capture_id))
        return capture_id

    async def submit_follow_up(self, capture_id: str, answer: str) -> None:
        """Record the nudge answer and kick off Pass 2 (re-organize + replace). 202 semantics."""
        record = await self._store.get(capture_id)
        if record is None:
            raise CaptureNotFound(capture_id)
        if not record.follow_up_question or record.follow_up_answer:
            raise FollowUpNotPending(capture_id)
        await self._store.set_follow_up_answer(capture_id, answer)
        self._spawn(self._reprocess_with_follow_up(capture_id))

    async def sweep_orphans(self) -> int:
        """Boot recovery: mark interrupted in-flight captures failed (retryable)."""
        count = await self._store.sweep_orphans(_ORPHAN_ERROR)
        if count:
            logger.warning("swept %d interrupted capture(s) to failed at boot", count)
        return count

    async def drain(self) -> None:
        """Await any in-flight background tasks (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- pipeline core ------------------------------------------------------------------

    async def _process(self, capture_id: str) -> None:
        try:
            record = await self._store.get(capture_id)
            if record is None:
                logger.error("capture %s vanished before processing", capture_id)
                return

            transcript = record.raw_text or ""
            if record.kind == KIND_VOICE:
                await self._store.mark_status(capture_id, TRANSCRIBING)
                try:
                    transcript = await self._transcribe(record.audio_path)
                except ProviderUnavailable as exc:
                    # STT has no fallback in M1; failure is infra → failed + retryable.
                    await self._store.mark_failed(capture_id, f"transcription failed: {exc}")
                    return
                await self._store.set_raw_text(capture_id, transcript)

            await self._store.mark_status(capture_id, ORGANIZING)
            organize = await self._organize(transcript)

            created_local = self._local(record.created_at)
            paths = await asyncio.to_thread(
                self._notes.write_notes,
                list(organize.notes),
                capture_id=capture_id,
                created_local=created_local,
                source=record.kind,
            )
            await self._store.set_note_paths(capture_id, paths)
            # `written` reflects notes actually on disk (matters for a future retry-resume).
            await self._store.mark_status(capture_id, WRITTEN)

            # Index step is a no-op stub in M1 (notes/chunks stay empty until M2); it only
            # advances the status. Keeps the supersede path pure filesystem+git.
            await self._store.mark_status(capture_id, INDEXED)
            await self._backup.request_commit(f"capture {capture_id}")

            # Trailing, non-blocking nudge — notes have already landed. Skipped on the Inbox
            # fallback path (there is no understanding to dig into — ADR-019 §1).
            if not organize.used_fallback:
                await self._generate_nudge(capture_id, organize.notes)
        except Exception as exc:  # noqa: BLE001 — must never crash the service (rule 7)
            logger.exception("capture %s pipeline failed", capture_id)
            await self._safe_mark_failed(capture_id, f"{type(exc).__name__}: {exc}")

    async def _reprocess_with_follow_up(self, capture_id: str) -> None:
        try:
            record = await self._store.get(capture_id)
            if record is None:
                return
            combined = (
                f"{record.raw_text or ''}\n\n"
                f"[Follow-up] {record.follow_up_question}\n"
                f"[Answer] {record.follow_up_answer}"
            ).strip()

            await self._store.mark_status(capture_id, ORGANIZING)
            organize = await self._organize(combined)
            if organize.used_fallback:
                # Organize chain unavailable — do NOT destroy the good Pass-1 notes. Keep them
                # intact and fail retryably so the answer can be re-applied later. ADR-019 §2
                # is about *enriching* the set; degrading a good set to an Inbox dump would
                # violate that intent.
                await self._store.mark_failed(
                    capture_id,
                    "follow-up re-organize unavailable; original notes kept (retry to re-apply)",
                )
                return

            # Soft-delete the Pass-1 notes, then write the enriched set and REPLACE note_paths
            # (ADR-019 §2). Removal is a filesystem unlink; git history retains the content.
            await asyncio.to_thread(self._notes.remove_notes, list(record.note_paths))
            created_local = self._local(record.created_at)
            paths = await asyncio.to_thread(
                self._notes.write_notes,
                list(organize.notes),
                capture_id=capture_id,
                created_local=created_local,
                source=record.kind,
            )
            await self._store.set_note_paths(capture_id, paths)
            await self._store.mark_status(capture_id, WRITTEN)

            await self._store.mark_status(capture_id, INDEXED)
            await self._backup.request_commit(f"capture {capture_id} follow-up")
            # No second nudge — ADR-019 ships exactly one.
        except Exception as exc:  # noqa: BLE001
            logger.exception("capture %s follow-up reprocess failed", capture_id)
            await self._safe_mark_failed(capture_id, f"{type(exc).__name__}: {exc}")

    async def _organize(self, text: str) -> OrganizeResult:
        """Run the organize chain and validate; unusable output → single Inbox note."""
        # Token replacement (not str.format): the prompt embeds literal JSON braces.
        system = ORGANIZER_SYSTEM_PROMPT.replace(
            "{planes}", ", ".join(self._settings.planes)
        ).replace("{inbox}", self._settings.inbox_plane)
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=text),
        ]
        try:
            result = await self._registry.distill(messages)
        except ProviderUnavailable as exc:
            logger.warning("organize chain exhausted, using Inbox fallback: %s", exc)
            return self._inbox_result(text)

        notes = validate_organizer_output(
            parse_organizer_json(result.text),
            planes=list(self._settings.planes),
            inbox_plane=self._settings.inbox_plane,
            max_notes=self._settings.organizer_max_notes,
            max_tags=self._settings.organizer_max_tags,
        )
        if not notes:
            logger.info("organize produced no valid notes, using Inbox fallback")
            return self._inbox_result(text)
        return OrganizeResult(notes=notes, used_fallback=False)

    def _inbox_result(self, text: str) -> OrganizeResult:
        note = inbox_fallback_note(text, inbox_plane=self._settings.inbox_plane)
        return OrganizeResult(notes=(note,), used_fallback=True)

    async def _generate_nudge(
        self, capture_id: str, notes: tuple[OrganizerNote, ...]
    ) -> None:
        """Best-effort trailing nudge, generated from the held organize result (ADR-019 §1).

        MUST never fail the capture: it is already ``indexed`` with notes on disk, so ANY error
        here (chain unavailable, an errant store write) is swallowed and logged — never
        propagated to flip the capture to ``failed``.
        """
        try:
            summary = "\n\n".join(f"{n.title}\n{n.body}" for n in notes)
            result = await self._registry.distill(
                [
                    ChatMessage(role="system", content=NUDGE_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=summary),
                ]
            )
            question = result.text.strip()[:_MAX_NUDGE_CHARS].strip()
            if question:
                await self._store.set_follow_up_question(capture_id, question)
        except ProviderUnavailable as exc:
            logger.info("nudge generation skipped (chain unavailable): %s", exc)
        except Exception:  # noqa: BLE001 — a nudge must never fail an already-indexed capture
            logger.exception("nudge generation failed for capture %s (ignored)", capture_id)

    async def _transcribe(self, audio_path: str | None) -> str:
        if not audio_path:
            raise ProviderUnavailable("voice capture has no stored audio")
        data = await asyncio.to_thread(self._read_audio, audio_path)
        return await self._registry.transcribe(data, filename=audio_path)

    # --- helpers ------------------------------------------------------------------------

    def _local(self, created_at: datetime | None) -> datetime:
        """Convert a (UTC) DB timestamp to the app's local tz for vault-facing formatting."""
        if created_at is None:
            created_at = datetime.now(self._tz)
        return created_at.astimezone(self._tz)

    def _audio_file(self, stored_name: str) -> Path:
        return Path(self._settings.data_path) / stored_name

    def _write_audio(self, stored_name: str, data: bytes) -> None:
        path = self._audio_file(stored_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _read_audio(self, stored_name: str) -> bytes:
        return self._audio_file(stored_name).read_bytes()

    async def _safe_mark_failed(self, capture_id: str, error: str) -> None:
        try:
            await self._store.mark_failed(capture_id, error)
        except Exception:  # noqa: BLE001 — last-ditch; DB may be down
            logger.exception("could not mark capture %s failed", capture_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _audio_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
