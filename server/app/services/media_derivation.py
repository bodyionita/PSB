"""Media-understanding stage (04-pipelines §2, ADR-057 §3/§5).

Derives text from a stored media item: **photo → vision description** (the `vision` routing group,
ADR-057 §4), **voice → STT** (the Groq→OpenAI chain, ADR-020). Video arrives pre-summarized at
import (the recorded ADR-057 §2 exception — no server video stage), so this stage never derives it.

The stage is **status-tracked and resumable** (ADR-057 §3): each attempt bumps ``attempts``; a
success writes ``derived`` + the text/model; a failure with retries left stays ``pending`` (a later
pass retries); once ``attempts`` reaches ``media_derive_max_attempts`` the item is marked
``unavailable`` and downstream consumers render an explicit placeholder rather than blocking. Giving
up is reversible — raw is kept, so :meth:`rederive` can reset ``unavailable`` items (or an explicit
list) to ``pending`` and try again, e.g. after the VLM improves.

Idempotent (rule 6): a ``derived`` item is skipped. Best-effort/visible (rule 7): a bad item never
crashes a batch; the failure is recorded on the row (``error``) and in the run.

One description contract (ADR-057 §5, binding): compact + factual, transcribe legible text
**verbatim**, and — for a chat screenshot — use the screenshot's **own internal attribution**
(names + bubble alignment), never presenting contained messages as the sharer's own words. That is
the *vision layer*; the distiller/organizer layer (§5 second bullet) lives in the ingest prompts.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from ..providers.base import ChatMessage, ProviderUnavailable
from ..providers.registry import ProviderRegistry
from .agent_runs import FAILED as RUN_FAILED
from .agent_runs import SUCCEEDED as RUN_SUCCEEDED
from .agent_runs import AgentRunStore
from .media_store import (
    DERIVED,
    KIND_PHOTO,
    KIND_VIDEO,
    KIND_VOICE,
    PENDING,
    UNAVAILABLE,
    MediaFiles,
    MediaStore,
)
from .model_routing import ModelRoutingService

logger = logging.getLogger(__name__)

# The `vision` routing group the photo description routes through (ADR-057 §4).
VISION_GROUP = "vision"

# Default content types when a row didn't record one (defensive — T3 stores the real mime).
_DEFAULT_MIME = {KIND_PHOTO: "image/jpeg", KIND_VOICE: "audio/mpeg"}

# Explicit placeholders for an `unavailable` item (ADR-057 §3) — the contract home; downstream
# consumers (the capture/node surface T4, the session transcript M9.5) render these.
PHOTO_PLACEHOLDER = "<photo — description unavailable>"
VOICE_PLACEHOLDER = "<voice note — transcript unavailable>"
VIDEO_PLACEHOLDER = "<video — summary unavailable>"
_PLACEHOLDERS = {
    KIND_PHOTO: PHOTO_PLACEHOLDER,
    KIND_VOICE: VOICE_PLACEHOLDER,
    KIND_VIDEO: VIDEO_PLACEHOLDER,
}


def placeholder(kind: str) -> str:
    """The explicit placeholder a downstream consumer renders for an ``unavailable`` media item."""
    return _PLACEHOLDERS.get(kind, "<media — unavailable>")


# The photo description contract (ADR-057 §5 — vision layer). Binding: compact + factual, verbatim
# legible text, and the two-part screenshot rule (say it's a screenshot; attribute contained
# messages by the SCREENSHOT's own names + bubble alignment, never as the sharer's words).
MEDIA_DESCRIPTION_SYSTEM_PROMPT = """\
You describe an image for a personal knowledge base. Output ONE compact, factual description of
what the image literally shows — no preamble, no markdown, no guessing who unlabeled people are.

Rules:
- Be concise and concrete. Describe only what is visible.
- Transcribe ANY legible text in the image VERBATIM — many images are screenshots and the text is
  the whole point. Preserve wording, names and numbers exactly. If text is cut off or illegible,
  say so; never invent it.
- If the image is a screenshot of a chat or messaging conversation, state that it is a screenshot,
  and transcribe the messages using the SCREENSHOT'S OWN attribution: the names shown in the image
  and the left/right bubble alignment. NEVER present those messages as the words of whoever shared
  or sent the screenshot — they belong to the people inside the image.
"""

_DESCRIBE_INSTRUCTION = "Describe this image following the rules above."


@dataclass(frozen=True)
class DeriveOutcome:
    """The result of deriving one media item (aggregated into a re-derive run summary)."""

    media_id: str
    kind: str
    status: str  # `derived` | `unavailable` | `skipped`
    model_used: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RederiveOutcome:
    """A targeted re-derive pass (ADR-057 §3): how many items were revisited + where they landed."""

    considered: int
    derived: int
    unavailable: int
    skipped: int


class MediaDerivationService:
    """Derive text from stored media (photo→vision, voice→STT), status-tracked + resumable."""

    def __init__(
        self,
        *,
        store: MediaStore,
        files: MediaFiles,
        routing: ModelRoutingService,
        registry: ProviderRegistry,
        run_store: AgentRunStore,
        max_attempts: int,
        rederive_max_per_run: int,
    ) -> None:
        self._store = store
        self._files = files
        self._routing = routing
        self._registry = registry
        self._runs = run_store
        self._max_attempts = max(1, max_attempts)
        self._rederive_max_per_run = max(1, rederive_max_per_run)

    async def derive_one(self, media_id: str) -> DeriveOutcome:
        """Derive one media item from its stored raw, advancing its status (ADR-057 §3).

        Idempotent: a ``derived`` item is skipped. A failure with retries left keeps the item
        ``pending`` (retryable); the ``media_derive_max_attempts``-th failure marks it
        ``unavailable``. Never raises — the outcome carries any error (rule 7)."""
        record = await self._store.get(media_id)
        if record is None:
            return DeriveOutcome(media_id=media_id, kind="", status="skipped", error="not found")
        if record.status == DERIVED:
            return DeriveOutcome(
                media_id=media_id, kind=record.kind, status="skipped", model_used=record.model_used
            )

        attempt = record.attempts + 1
        try:
            text, model = await self._derive_text(record)
            text = (text or "").strip()
            if not text:
                raise ProviderUnavailable("empty derivation result")
        except ProviderUnavailable as exc:
            return await self._record_failure(media_id, record.kind, attempt, str(exc))
        except Exception as exc:  # noqa: BLE001 — one bad item must not crash a batch (rule 7)
            logger.exception("media derivation of %s failed", media_id)
            return await self._record_failure(
                media_id, record.kind, attempt, f"{type(exc).__name__}: {exc}"
            )

        await self._store.mark_derived(
            media_id, derived_text=text, model_used=model, attempts=attempt
        )
        return DeriveOutcome(media_id=media_id, kind=record.kind, status=DERIVED, model_used=model)

    async def derive_until_settled(self, media_id: str) -> DeriveOutcome:
        """Drive one item to a **terminal** derivation state within a single call — the ad-hoc
        image-capture trigger (M9 T3, ADR-057 §3/§6).

        :meth:`derive_one` does one bounded attempt; a retryable failure leaves the item
        ``pending``. The connector path (M9.5) re-invokes derive on a schedule (with backoff), but
        an interactive photo capture wants a prompt resolution, so here the per-invocation retries
        run back-to-back: loop while the outcome is ``pending``, so a persistent failure walks retry
        → ``unavailable`` (→ explicit placeholder downstream) **without a human**, and a transient
        one recovers. Each attempt bumps ``attempts``, so at most ``max_attempts`` calls land on
        ``derived`` / ``unavailable`` / ``skipped``; the loop guard bounds it even if a store
        miscounts. ADR-057 §3 retry *backoff* is deferred (recorded in 08 §M9). Never raises —
        inherits ``derive_one``'s best-effort contract (rule 7)."""
        outcome = await self.derive_one(media_id)
        attempts_left = self._max_attempts
        while outcome.status == PENDING and attempts_left > 0:
            attempts_left -= 1
            outcome = await self.derive_one(media_id)
        return outcome

    async def _derive_text(self, record) -> tuple[str, str | None]:
        """Route by kind to the right understanding path; return ``(text, model_used)``."""
        if record.kind == KIND_PHOTO:
            return await self._describe_photo(record)
        if record.kind == KIND_VOICE:
            return await self._transcribe_voice(record)
        if record.kind == KIND_VIDEO:
            # Video is summary-only, produced at import (ADR-057 §2) — there is no server video
            # stage. A pending video row is a data anomaly; fail it clearly (never guessed).
            raise ProviderUnavailable(
                "video summaries are produced at import (ADR-057 §2); no server video derivation"
            )
        raise ProviderUnavailable(f"unknown media kind: {record.kind!r}")

    async def _describe_photo(self, record) -> tuple[str, str | None]:
        if not record.file_path:
            raise ProviderUnavailable("photo has no stored file")
        data = await self._files.read_async(record.file_path)
        mime = record.mime_type or _DEFAULT_MIME[KIND_PHOTO]
        data_uri = _data_uri(mime, data)
        messages = [
            ChatMessage(role="system", content=MEDIA_DESCRIPTION_SYSTEM_PROMPT),
            ChatMessage(role="user", content=_DESCRIBE_INSTRUCTION),
        ]
        result = await self._routing.complete(VISION_GROUP, messages, images=[data_uri])
        return result.text, result.model_used or None

    async def _transcribe_voice(self, record) -> tuple[str, str | None]:
        if not record.file_path:
            raise ProviderUnavailable("voice note has no stored file")
        data = await self._files.read_async(record.file_path)
        filename = record.file_path.rsplit("/", 1)[-1]
        result = await self._registry.transcribe(data, filename=filename)
        return result.text, result.model_used or None

    async def _record_failure(
        self, media_id: str, kind: str, attempt: int, error: str
    ) -> DeriveOutcome:
        if attempt >= self._max_attempts:
            await self._store.mark_unavailable(media_id, error=error, attempts=attempt)
            return DeriveOutcome(media_id=media_id, kind=kind, status=UNAVAILABLE, error=error)
        await self._store.mark_retry(media_id, error=error, attempts=attempt)
        return DeriveOutcome(media_id=media_id, kind=kind, status=PENDING, error=error)

    async def rederive(self, *, media_ids: list[str] | None = None) -> RederiveOutcome:
        """Targeted re-derivation (ADR-057 §3): reset the selected items to ``pending`` (a fresh
        chance — attempts cleared) and derive each. Selection is either an explicit ``media_ids``
        list or, when omitted, the ``unavailable`` backlog (bounded by
        ``media_rederive_max_per_run``). Runs under its own ``agent_runs`` row (vision P8).
        Idempotent + never raises past its guard.
        """
        run_id = await self._start_run()
        try:
            if media_ids is not None:
                records = await self._store.get_many(media_ids)
            else:
                records = await self._store.list_by_status(
                    UNAVAILABLE, limit=self._rederive_max_per_run
                )
            ids = [r.id for r in records]
            await self._store.reset_to_pending(ids)

            derived = unavailable = skipped = 0
            for media_id in ids:
                outcome = await self.derive_one(media_id)
                if outcome.status == DERIVED:
                    derived += 1
                elif outcome.status == UNAVAILABLE:
                    unavailable += 1
                else:
                    skipped += 1  # `pending` (retries left) or `skipped`

            result = RederiveOutcome(
                considered=len(ids),
                derived=derived,
                unavailable=unavailable,
                skipped=skipped,
            )
            await self._finish_run(run_id, result)
            return result
        except Exception as exc:  # noqa: BLE001 — the op ends as failed, it never crashes (rule 7)
            logger.exception("media re-derive run failed")
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}")
            raise

    # --- agent_runs plumbing (best-effort logging; never breaks the op) ------------------------

    async def _start_run(self) -> str | None:
        try:
            return await self._runs.start("media-rederive")
        except Exception:  # noqa: BLE001 — logging must not break the op (rule 7)
            logger.exception("could not open agent_runs row for media re-derive (logging degraded)")
            return None

    async def _finish_run(self, run_id: str | None, result: RederiveOutcome) -> None:
        if run_id is None:
            return
        summary = (
            f"re-derived {result.considered} media: {result.derived} recovered, "
            f"{result.unavailable} still unavailable, {result.skipped} pending/skipped"
        )
        try:
            await self._runs.finish(
                run_id, status=RUN_SUCCEEDED, summary=summary, details=result.__dict__
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not close media re-derive run %s (logging degraded)", run_id)

    async def _fail_run(self, run_id: str | None, error: str) -> None:
        if run_id is None:
            return
        try:
            await self._runs.finish(run_id, status=RUN_FAILED, error=error)
        except Exception:  # noqa: BLE001
            logger.exception("could not close media re-derive run %s (logging degraded)", run_id)


def _data_uri(mime: str, data: bytes) -> str:
    """A base64 ``data:`` URI for an OpenAI-compatible ``image_url`` part (ADR-057 §4)."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def build_media_derivation_service(
    *,
    settings,
    store: MediaStore,
    files: MediaFiles,
    routing: ModelRoutingService,
    registry: ProviderRegistry,
    run_store: AgentRunStore,
) -> MediaDerivationService:
    return MediaDerivationService(
        store=store,
        files=files,
        routing=routing,
        registry=registry,
        run_store=run_store,
        max_attempts=settings.media_derive_max_attempts,
        rederive_max_per_run=settings.media_rederive_max_per_run,
    )
