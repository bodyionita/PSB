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
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..capture.notes import NoteWriter
from ..capture.organizer import (
    NUDGE_SYSTEM_PROMPT,
    ORGANIZER_SYSTEM_PROMPT,
    OrganizeResult,
    inbox_fallback_note,
    parse_organizer_json,
    validate_organizer_output,
)
from ..config import Settings
from ..providers.base import ChatMessage, ProviderUnavailable, TranscriptResult
from ..providers.registry import ProviderRegistry
from .agent_runs import (
    FAILED as RUN_FAILED,
)
from .agent_runs import (
    SKIPPED as RUN_SKIPPED,
)
from .agent_runs import (
    SUCCEEDED as RUN_SUCCEEDED,
)
from .agent_runs import (
    AgentRunStore,
)
from .capture_store import (
    FAILED,
    INDEXED,
    KIND_TEXT,
    KIND_VOICE,
    ORGANIZING,
    RECEIVED,
    TRANSCRIBING,
    WRITTEN,
    CaptureRecord,
    CaptureStore,
)
from .vault_backup import VaultBackup

logger = logging.getLogger(__name__)

# Audio container extensions accepted by POST /capture/voice (03-api.md).
ALLOWED_AUDIO_EXTS = frozenset({"m4a", "webm", "ogg", "mp3", "wav"})
_ORPHAN_ERROR = "interrupted by restart"
_MAX_NUDGE_CHARS = 300  # a one-line question; guards against a runaway model reply


class _Interaction:
    """Accumulates the per-capture model-call detail for the ``agent_runs`` row (ADR-021).

    Populated as the pipeline runs; serialised into ``details`` (+ top-level ``model_used`` /
    ``fallback_used``) when the run is closed. Purely a logging concern — it never influences
    capture behaviour.
    """

    def __init__(self, *, capture_id: str, kind: str) -> None:
        self._start = time.monotonic()
        self.capture_id = capture_id
        self.kind = kind
        self.stt: dict[str, Any] | None = None
        self.organize: dict[str, Any] | None = None
        self.nudge: dict[str, Any] | None = None
        self.timings_ms: dict[str, int] = {}
        # Top-level agent_runs columns: organize model wins; any step's fallback flips the flag.
        self.model_used: str | None = None
        self.fallback_used: bool = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def details(self) -> dict[str, Any]:
        self.timings_ms.setdefault("total", self.elapsed_ms())
        return {
            "capture_id": self.capture_id,
            "kind": self.kind,
            "stt": self.stt,
            "organize": self.organize,
            "nudge": self.nudge,
            "timings_ms": self.timings_ms,
        }


class CaptureError(Exception):
    """Base for capture problems surfaced to the API layer."""


class UnsupportedAudio(CaptureError):
    """The uploaded audio is too large or an unsupported container."""


class CaptureNotFound(CaptureError):
    """No capture with the given id."""


class FollowUpNotPending(CaptureError):
    """The capture has no pending follow-up question to answer (409)."""


class NotRetryable(CaptureError):
    """Retry was requested on a capture that is not ``failed`` (409)."""


class CapturePipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        store: CaptureStore,
        registry: ProviderRegistry,
        note_writer: NoteWriter,
        vault_backup: VaultBackup,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._notes = note_writer
        self._backup = vault_backup
        self._runs = run_store
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

    async def get(self, capture_id: str) -> CaptureRecord | None:
        """Read a capture's current pipeline state (GET /captures/{id})."""
        return await self._store.get(capture_id)

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        """Recent captures, newest first, for the capture-screen strip (GET /captures)."""
        return await self._store.list_recent(limit)

    async def submit_follow_up(self, capture_id: str, answer: str) -> None:
        """Record the nudge answer and kick off Pass 2 (re-organize + replace). 202 semantics."""
        record = await self._store.get(capture_id)
        if record is None:
            raise CaptureNotFound(capture_id)
        if not record.follow_up_question or record.follow_up_answer:
            raise FollowUpNotPending(capture_id)
        await self._store.set_follow_up_answer(capture_id, answer)
        self._spawn(self._reprocess_with_follow_up(capture_id))

    async def retry_capture(self, capture_id: str) -> None:
        """Re-run a ``failed`` capture from its first incomplete step (03-api; 409 otherwise).

        The raw input is always still on disk / in the row (never-lose), so retry is safe to
        re-drive. Two cases, kept idempotent (rule 6):

        * A **follow-up** answer was recorded but its Pass 2 didn't land (chain was down — the
          notes were deliberately kept). Re-run Pass 2; it re-organizes original+answer and
          only replaces the notes on success, so re-applying is safe.
        * Otherwise the main pipeline failed (STT down, vault write, or a boot-swept orphan).
          Remove the **recorded** notes (``note_paths``) first so re-running can't duplicate
          that set, then re-drive ``_process`` from the top. (A note that landed in a batch
          that crashed *before* ``set_note_paths`` recorded it is not tracked here; that
          partial-write edge is a known follow-up — see 08 M1 progress.)
        """
        record = await self._store.get(capture_id)
        if record is None:
            raise CaptureNotFound(capture_id)
        if record.status != FAILED:
            raise NotRetryable(capture_id)

        await self._store.reset_for_retry(capture_id)
        if record.follow_up_answer:
            self._spawn(self._reprocess_with_follow_up(capture_id))
            return
        if record.note_paths:
            await asyncio.to_thread(self._notes.remove_notes, list(record.note_paths))
            await self._store.set_note_paths(capture_id, [])
        self._spawn(self._process(capture_id))

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
        # One agent_runs row per capture run (ADR-021) — the queryable interaction log. Opening
        # it must never break the pipeline (rule 7), so a store failure just yields run_id=None.
        run_id = await self._start_run()
        inter = _Interaction(capture_id=capture_id, kind="")
        try:
            record = await self._store.get(capture_id)
            if record is None:
                logger.error("capture %s vanished before processing", capture_id)
                await self._finish_run(
                    run_id, RUN_SKIPPED, inter, summary="capture vanished before processing"
                )
                return
            inter.kind = record.kind

            transcript = record.raw_text or ""
            if record.kind == KIND_VOICE:
                await self._store.mark_status(capture_id, TRANSCRIBING)
                t0 = time.monotonic()
                try:
                    stt = await self._transcribe(record.audio_path)
                except ProviderUnavailable as exc:
                    # Whole STT chain exhausted (both providers down/limited) → infra failure,
                    # capture is retryable; the audio is on disk (never-lose).
                    inter.stt = {"provider": None, "fallback_used": False, "error": str(exc)}
                    inter.timings_ms["transcribe"] = int((time.monotonic() - t0) * 1000)
                    await self._store.mark_failed(capture_id, f"transcription failed: {exc}")
                    await self._finish_run(
                        run_id, RUN_FAILED, inter, error=f"transcription failed: {exc}"
                    )
                    return
                inter.timings_ms["transcribe"] = int((time.monotonic() - t0) * 1000)
                inter.stt = {
                    "provider": stt.model_used,
                    "fallback_used": stt.fallback_used,
                    "error": None,
                }
                inter.fallback_used = inter.fallback_used or stt.fallback_used
                transcript = stt.text
                await self._store.set_raw_text(capture_id, transcript)

            await self._store.mark_status(capture_id, ORGANIZING)
            t1 = time.monotonic()
            organize = await self._organize(transcript)
            inter.timings_ms["organize"] = int((time.monotonic() - t1) * 1000)
            inter.organize = {
                "model": organize.model_used or None,
                "fallback_used": organize.provider_fallback_used,
                "inbox_fallback": organize.used_fallback,
            }
            inter.model_used = organize.model_used or inter.model_used
            inter.fallback_used = inter.fallback_used or organize.provider_fallback_used

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
            # fallback path (there is no understanding to dig into — ADR-019 §1). Sourced from
            # the raw capture (not the notes) so it matches the person's language.
            if not organize.used_fallback:
                nudge_model = await self._generate_nudge(capture_id, transcript)
                inter.nudge = {"model": nudge_model}

            await self._finish_run(
                run_id, RUN_SUCCEEDED, inter, summary=self._run_summary(inter, organize)
            )
        except Exception as exc:  # noqa: BLE001 — must never crash the service (rule 7)
            logger.exception("capture %s pipeline failed", capture_id)
            await self._finish_run(run_id, RUN_FAILED, inter, error=f"{type(exc).__name__}: {exc}")
            await self._safe_mark_failed(capture_id, f"{type(exc).__name__}: {exc}")

    async def _reprocess_with_follow_up(self, capture_id: str) -> None:
        # Pass 2 is a second run over the same capture (ADR-019 §2) — its own agent_runs row so
        # the re-organize model interaction is visible too (ADR-021).
        run_id = await self._start_run()
        inter = _Interaction(capture_id=capture_id, kind="")
        try:
            record = await self._store.get(capture_id)
            if record is None:
                await self._finish_run(
                    run_id, RUN_SKIPPED, inter, summary="follow-up: capture vanished"
                )
                return
            inter.kind = f"{record.kind}-followup"
            combined = (
                f"{record.raw_text or ''}\n\n"
                f"[Follow-up] {record.follow_up_question}\n"
                f"[Answer] {record.follow_up_answer}"
            ).strip()

            await self._store.mark_status(capture_id, ORGANIZING)
            t0 = time.monotonic()
            organize = await self._organize(combined)
            inter.timings_ms["organize"] = int((time.monotonic() - t0) * 1000)
            inter.organize = {
                "model": organize.model_used or None,
                "fallback_used": organize.provider_fallback_used,
                "inbox_fallback": organize.used_fallback,
            }
            inter.model_used = organize.model_used or inter.model_used
            inter.fallback_used = organize.provider_fallback_used
            if organize.used_fallback:
                # Organize chain unavailable — do NOT destroy the good Pass-1 notes. Keep them
                # intact and fail retryably so the answer can be re-applied later. ADR-019 §2
                # is about *enriching* the set; degrading a good set to an Inbox dump would
                # violate that intent.
                msg = "follow-up re-organize unavailable; original notes kept (retry to re-apply)"
                await self._store.mark_failed(capture_id, msg)
                await self._finish_run(run_id, RUN_FAILED, inter, error=msg)
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
            await self._finish_run(
                run_id, RUN_SUCCEEDED, inter, summary=self._run_summary(inter, organize)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("capture %s follow-up reprocess failed", capture_id)
            await self._finish_run(run_id, RUN_FAILED, inter, error=f"{type(exc).__name__}: {exc}")
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
        return OrganizeResult(
            notes=notes,
            used_fallback=False,
            model_used=result.model_used,
            provider_fallback_used=result.fallback_used,
        )

    def _inbox_result(self, text: str) -> OrganizeResult:
        note = inbox_fallback_note(text, inbox_plane=self._settings.inbox_plane)
        return OrganizeResult(notes=(note,), used_fallback=True)

    async def _generate_nudge(self, capture_id: str, capture_text: str) -> str | None:
        """Best-effort trailing nudge, generated from the person's ORIGINAL capture (ADR-019 §1).

        Sourced from the raw capture text (not the organized notes) so the question lands in the
        same language the person used and stays faithful to what they actually said. MUST never
        fail the capture: it is already ``indexed`` with notes on disk, so ANY error here (chain
        unavailable, an errant store write) is swallowed and logged — never propagated to flip
        the capture to ``failed``. Returns the model that generated the nudge (for the
        interaction log), or ``None`` if it was skipped.
        """
        try:
            result = await self._registry.distill(
                [
                    ChatMessage(role="system", content=NUDGE_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=capture_text),
                ]
            )
            question = result.text.strip()[:_MAX_NUDGE_CHARS].strip()
            if question:
                await self._store.set_follow_up_question(capture_id, question)
            return result.model_used or None
        except ProviderUnavailable as exc:
            logger.info("nudge generation skipped (chain unavailable): %s", exc)
            return None
        except Exception:  # noqa: BLE001 — a nudge must never fail an already-indexed capture
            logger.exception("nudge generation failed for capture %s (ignored)", capture_id)
            return None

    async def _transcribe(self, audio_path: str | None) -> TranscriptResult:
        if not audio_path:
            raise ProviderUnavailable("voice capture has no stored audio")
        data = await asyncio.to_thread(self._read_audio, audio_path)
        return await self._registry.transcribe(data, filename=audio_path)

    # --- agent_runs interaction log (ADR-021) -------------------------------------------

    async def _start_run(self) -> str | None:
        """Open the capture's agent_runs row. Never raises — logging is not the capture."""
        try:
            return await self._runs.start("capture")
        except Exception:  # noqa: BLE001 — a logging-store failure must not break the pipeline
            logger.exception("could not open agent_runs row for a capture (logging degraded)")
            return None

    async def _finish_run(
        self,
        run_id: str | None,
        status: str,
        inter: _Interaction,
        *,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        """Close the capture's agent_runs row. Never raises (rule 7)."""
        if run_id is None:
            return
        try:
            await self._runs.finish(
                run_id,
                status=status,
                summary=summary,
                details=inter.details(),
                error=error,
                model_used=inter.model_used,
                fallback_used=inter.fallback_used,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not close agent_runs row %s (logging degraded)", run_id)

    @staticmethod
    def _run_summary(inter: _Interaction, organize: OrganizeResult) -> str:
        """Human-readable one-liner for the activity feed (vision P8)."""
        note_word = "note" if len(organize.notes) == 1 else "notes"
        base = f"{inter.kind} capture → {len(organize.notes)} {note_word}"
        if organize.used_fallback:
            return f"{base} (Inbox fallback — organize unavailable)"
        if inter.fallback_used:
            return f"{base} (on {inter.model_used}, fallback)"
        return f"{base} (on {inter.model_used})" if inter.model_used else base

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
