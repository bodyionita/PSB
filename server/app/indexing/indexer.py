"""The indexer service — the real index step (04-pipelines §3, ADR-022/023).

Turns vault note files into the derived search index (``notes`` + ``chunks``). Per file:

    read → strip the ``sb:related`` block → sha256 ── unchanged? skip
         → parse frontmatter (plane/planes/tags/source/created)
         → chunk (02 §4) → batch-embed the chunks via nomic (``search_document:`` prefix)
         → per-note TRANSACTION: upsert note + replace chunks + notes.embedding = mean-pool

Robustness (ADR-022): the per-note transaction means a note is never half-indexed; on an embed
failure the note is **skipped and the run continues** (existing rows left intact, run marked
**partial**), so a later reindex re-does only still-stale notes (hash-skip). Indexing never
crashes its caller (rule 7). The relatedness graph is **not** touched here — it is recomputed
nightly only (ADR-023), never on the real-time capture write.

``content_hash`` covers frontmatter + body *minus* the ``sb:related`` block (02 §3): metadata
edits reindex, but the graph's own block rewrites never re-trigger one (the feedback-loop fix).
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..providers.base import ProviderUnavailable
from ..providers.registry import ProviderRegistry
from .chunking import chunk_note, strip_related_block
from .frontmatter import parse_note_metadata
from .store import IndexStore, NoteChunk, NoteUpsert

logger = logging.getLogger(__name__)

# nomic asymmetric task prefix for the indexing side (ADR-022); the search side uses
# ``search_query:``. The note title is prepended as context for each chunk (02 §4).
_DOCUMENT_PREFIX = "search_document:"


@dataclass(frozen=True)
class IndexOutcome:
    """Result of an index run over a set of paths (feeds the ``reindex`` agent_runs row later)."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """True when at least one note was skipped by an embed failure (run status ``partial``)."""
        return self.failed > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "deleted": self.deleted,
            "partial": self.partial,
            "failures": list(self.failures),
        }


class NoteIndexer(Protocol):
    """The indexing surface the capture/ingestion pipelines depend on."""

    async def index_paths(self, vault_paths: list[str]) -> IndexOutcome: ...


class Indexer:
    """Indexes vault note files into ``notes`` + ``chunks``. The single owner of embed-on-index."""

    def __init__(
        self, *, settings: Settings, store: IndexStore, registry: ProviderRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._vault_root = Path(settings.vault_path)
        self._ignore = set(settings.vault_ignore)

    async def index_paths(self, vault_paths: list[str]) -> IndexOutcome:
        """Index a specific set of notes (the capture/ingestion write path).

        Skip-and-continue: a per-note failure (missing file, embed unavailable) is logged and
        counted, never raised — the rest of the batch still indexes.
        """
        indexed = skipped = failed = 0
        failures: list[str] = []
        for vault_path in vault_paths:
            try:
                result = await self._index_one(vault_path)
            except Exception:  # noqa: BLE001 — one bad note must not abort the batch (rule 7)
                logger.exception("index: unexpected error on %s, skipping", vault_path)
                result = _Result.FAILED
            if result is _Result.INDEXED:
                indexed += 1
            elif result is _Result.SKIPPED:
                skipped += 1
            else:
                failed += 1
                failures.append(vault_path)
        return IndexOutcome(
            indexed=indexed, skipped=skipped, failed=failed, failures=failures
        )

    async def reindex_all(self) -> IndexOutcome:
        """Full rescan: index every vault ``*.md`` and reconcile deletions (04 §3).

        DB rows whose files no longer exist are removed (``chunks``/``note_links`` cascade); a
        rename is a delete of the old path + insert of the new. Idempotent. Does **not** compute
        the relatedness graph — that is a nightly-only step (ADR-023), layered on by the caller.
        """
        paths = await asyncio.to_thread(self._scan_vault)
        outcome = await self.index_paths(paths)
        stale = await self._store.list_indexed_paths() - set(paths)
        deleted = await self._store.delete_notes(sorted(stale))
        if deleted:
            logger.info("reindex removed %d note(s) whose vault files are gone", deleted)
        return IndexOutcome(
            indexed=outcome.indexed,
            skipped=outcome.skipped,
            failed=outcome.failed,
            deleted=deleted,
            failures=outcome.failures,
        )

    @staticmethod
    def content_hash(raw_text: str) -> str:
        """sha256 over frontmatter + body, excluding the ``sb:related`` machine block (02 §3).

        Surrounding whitespace is normalized (``strip``) so that adding/updating/removing the
        trailing ``sb:related`` block — which leaves stray blank lines where it was — never shifts
        the hash. That is what stops the graph's own nightly writes from re-triggering a reindex
        (the feedback-loop fix, ADR-023). Frontmatter is *kept*, so tag/plane edits still reindex.
        """
        stripped = strip_related_block(raw_text).strip()
        return hashlib.sha256(stripped.encode("utf-8")).hexdigest()

    # --- per-note core ------------------------------------------------------------------

    async def _index_one(self, vault_path: str) -> _Result:
        read = await asyncio.to_thread(self._read_note, vault_path)
        if read is None:
            logger.warning("index: note %s missing on disk, skipping", vault_path)
            return _Result.FAILED
        raw_text, mtime = read

        content_hash = self.content_hash(raw_text)
        if await self._store.get_content_hash(vault_path) == content_hash:
            return _Result.SKIPPED  # unchanged since last index

        meta = parse_note_metadata(
            raw_text, vault_path=vault_path, fallback_created=mtime
        )
        chunks = chunk_note(
            raw_text,
            chunk_size=self._settings.chunk_size,
            chunk_overlap=self._settings.chunk_overlap,
        )

        note_chunks: list[NoteChunk] = []
        note_embedding: list[float] | None = None
        if chunks:
            try:
                vectors = await self._embed_chunks(chunks, title=meta.title)
            except ProviderUnavailable as exc:
                # Skip-and-continue (ADR-022): leave any existing rows intact, mark the run
                # partial. A later reindex retries this still-stale note.
                logger.warning("index: embed failed for %s (%s); skipping", vault_path, exc)
                return _Result.FAILED
            note_chunks = [
                NoteChunk(index=i, content=chunk, embedding=vector)
                for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
            ]
            note_embedding = _mean_pool(vectors)

        await self._store.upsert_note(
            NoteUpsert(
                vault_path=vault_path,
                content_hash=content_hash,
                title=meta.title,
                plane=meta.plane,
                planes=meta.planes,
                tags=meta.tags,
                source=meta.source,
                source_ref=meta.source_ref,
                note_created_at=meta.created,
                embedding=note_embedding,
                chunks=note_chunks,
            )
        )
        return _Result.INDEXED

    async def _embed_chunks(self, chunks: list[str], *, title: str | None) -> list[list[float]]:
        """Batch-embed a note's chunks in one call, with bounded 429/unavailable backoff.

        Each chunk is prefixed ``search_document: {title}\\n\\n{chunk}`` — the asymmetric nomic
        task prefix is mandatory (ADR-022) and the title gives each chunk note-level context (02
        §4). One call per note; the whole note's chunks share the retry.
        """
        prefix = f"{_DOCUMENT_PREFIX} {title or ''}"
        inputs = [f"{prefix}\n\n{chunk}" for chunk in chunks]
        attempts = max(1, self._settings.embed_max_attempts)
        for attempt in range(attempts):
            try:
                result = await self._registry.embed(inputs)
                return result.vectors
            except ProviderUnavailable:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(self._settings.embed_retry_backoff_seconds * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    # --- vault filesystem ---------------------------------------------------------------

    def _read_note(self, vault_path: str) -> tuple[str, datetime] | None:
        """Read a note's text + mtime (used as the ``created`` fallback). ``None`` if it's gone."""
        path = self._vault_root / Path(*vault_path.split("/"))
        try:
            raw_text = path.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return raw_text, mtime

    def _scan_vault(self) -> list[str]:
        """Every indexable ``*.md`` as a ``/``-separated vault-relative path (02 §1).

        Files under a ``VAULT_IGNORE`` segment (``.obsidian``, ``.trash``, ``.git``, ``templates``)
        are skipped at any depth.
        """
        if not self._vault_root.exists():
            return []
        paths: list[str] = []
        for abs_path in self._vault_root.rglob("*.md"):
            if not abs_path.is_file():
                continue
            rel_parts = abs_path.relative_to(self._vault_root).parts
            if self._ignore.intersection(rel_parts):
                continue
            paths.append("/".join(rel_parts))
        return sorted(paths)


class _Result(enum.Enum):
    """Per-note index outcome (internal — callers see the aggregated :class:`IndexOutcome`)."""

    INDEXED = enum.auto()
    SKIPPED = enum.auto()
    FAILED = enum.auto()


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of the chunk vectors → the note embedding (ADR-023; no extra call)."""
    count = len(vectors)
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            sums[i] += value
    return [value / count for value in sums]
