"""Tag consolidation service (ADR-024 §2) — the two-step ``POST /admin/tags/consolidate``.

Propose and apply are deliberately split so the human reviews the plan before any vault write:

  * :meth:`propose` — synchronous, **no writes**. Feeds the live tag vocabulary (with frequency)
    to the distill chain, parses + sanitises the merge plan against the current vocabulary, and
    returns ``{plan_id, merges}``. A down chain surfaces as ``ProviderUnavailable`` (→ 503).
  * :meth:`apply` — takes a (reviewed) plan back, opens an ``agent="tags-consolidate"`` run, and
    rewrites the affected notes' ``tags:`` frontmatter + reindexes them in the **background**, so
    the endpoint answers ``202 {run_id}`` (03-api §Admin). Reuses the same never-lose treatment as
    the reorganize path: atomic writes, git-tracked + revertible, skip-and-continue per note.

The plan is **stateless** on the server: apply carries the full plan, so nothing is stored
between the two calls (``plan_id`` is a correlation id for the UI / activity feed only).
Consolidation changes only frontmatter tags, not note bodies, so a touched note re-embeds to the
same vectors — the relatedness graph is untouched (recomputed nightly / on reindex, ADR-023).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..indexing.indexer import IndexOutcome, NoteIndexer
from ..providers.base import ChatMessage
from ..providers.registry import ProviderRegistry
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.vault_backup import BackupResult
from .consolidation import (
    CONSOLIDATION_SYSTEM_PROMPT,
    TagMerge,
    build_tag_mapping,
    clean_merges,
    parse_merge_plan,
    render_vocabulary,
    rewrite_note_tags,
)
from .store import TagStore

logger = logging.getLogger(__name__)

# agent_runs.agent name for the consolidation apply (visible in the activity feed, vision P8).
AGENT = "tags-consolidate"


@dataclass(frozen=True)
class ConsolidationProposal:
    """A propose result: an opaque correlation id + the sanitised merges to review (no writes)."""

    plan_id: str
    merges: list[TagMerge]


class VaultCommitter(Protocol):
    """The single vault-git op apply needs: a forced commit + push of the rewritten tags."""

    async def backup_now(self, reason: str = ...) -> BackupResult: ...


class TagConsolidationService:
    """Owns propose (LLM plan) + apply (rewrite frontmatter tags + reindex) for ADR-024 §2."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: TagStore,
        registry: ProviderRegistry,
        indexer: NoteIndexer,
        vault_backup: VaultCommitter,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._indexer = indexer
        self._backup = vault_backup
        self._runs = run_store
        self._vault_root = Path(settings.vault_path)
        # Strong refs to in-flight background apply task(s) so they are not GC'd mid-run.
        self._tasks: set[asyncio.Task] = set()

    # --- propose ----------------------------------------------------------------------------

    async def propose(self) -> ConsolidationProposal:
        """Compute merge candidates over the current tag vocabulary (no writes).

        Raises :class:`ProviderUnavailable` when the distill chain is exhausted (→ 503); an empty
        or non-conforming model reply yields an empty plan, not an error.
        """
        counts = await self._store.tag_counts(
            limit=self._settings.tags_consolidate_max_vocabulary
        )
        plan_id = uuid.uuid4().hex
        if len(counts) < 2:
            # Nothing to consolidate — don't spend a model call on a 0/1-tag vault.
            return ConsolidationProposal(plan_id=plan_id, merges=[])

        vocabulary = render_vocabulary([(c.tag, c.count) for c in counts])
        system = CONSOLIDATION_SYSTEM_PROMPT.replace("{vocabulary}", vocabulary)
        result = await self._registry.distill([ChatMessage(role="system", content=system)])

        allowed = {c.tag: c.count for c in counts}
        merges = clean_merges(parse_merge_plan(result.text), allowed=allowed)
        logger.info(
            "tags consolidate propose: %d merge group(s) over %d tags", len(merges), len(counts)
        )
        return ConsolidationProposal(plan_id=plan_id, merges=merges)

    # --- apply ------------------------------------------------------------------------------

    async def apply(self, plan: list[TagMerge]) -> str:
        """Open the run and rewrite the affected notes' tags in the background; return its run_id.

        The (reviewed) plan is re-sanitised (``allowed=None`` — trust the human's choices but still
        slugify + drop trivial/overlapping merges) before any write, so a malformed apply body is
        harmless. Returns the ``agent_runs`` id for the endpoint's ``202``.
        """
        merges = clean_merges([(m.canonical, list(m.variants)) for m in plan], allowed=None)
        mapping = build_tag_mapping(merges)
        run_id = await self._runs.start(AGENT)
        self._spawn(self._run_apply(run_id, mapping))
        return run_id

    async def _run_apply(self, run_id: str, mapping: dict[str, str]) -> None:
        """Rewrite every affected note's ``tags:`` line, reindex the changed ones, commit+push.

        Never raises (rule 7): a per-note read/write error is logged and skipped (skip-and-continue,
        mirroring the indexer/graph), and any unexpected failure ends the run ``failed`` with
        context. The vault is truth (rule 1), so a partial apply is safe to re-drive.
        """
        try:
            variants = sorted(mapping.keys())
            affected = await self._store.notes_with_any_tag(variants) if variants else []
            changed: list[str] = []
            failed = 0
            for note in affected:
                try:
                    if await self._rewrite_one(note.vault_path, mapping):
                        changed.append(note.vault_path)
                except Exception:  # noqa: BLE001 — one bad note must not abort the apply (rule 7)
                    logger.exception(
                        "tags consolidate: failed to rewrite %s, skipping", note.vault_path
                    )
                    failed += 1

            index = await self._indexer.index_paths(changed) if changed else IndexOutcome()
            committed = pushed = False
            if changed:
                backup = await self._backup.backup_now("tags consolidate")
                committed, pushed = backup.committed, backup.pushed

            summary = (
                f"tags consolidate: {len(changed)} note(s) rewritten across "
                f"{len(mapping)} variant(s) → {len(set(mapping.values()))} canonical tag(s)"
            )
            if failed:
                summary += f", {failed} skipped"
            if index.partial:
                # An embed skip left a rewritten note transiently stale in the index; the vault is
                # correct and the next reindex heals it (rule 1). Surface it as reindex does.
                summary += " (partial — embed failures)"
            logger.info("%s (pushed=%s)", summary, pushed)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "mapping": mapping,
                    "notes_rewritten": len(changed),
                    "notes_skipped": failed,
                    "index": index.as_dict(),
                    "commit": {"committed": committed, "pushed": pushed},
                },
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("tags consolidate apply failed")
            await self._safe_finish(run_id, exc)

    async def _rewrite_one(self, vault_path: str, mapping: dict[str, str]) -> bool:
        """Rewrite one note's ``tags:`` line if any variant is present; return True when written."""
        raw = await asyncio.to_thread(self._read, vault_path)
        if raw is None:
            logger.warning("tags consolidate: note %s missing on disk, skipping", vault_path)
            return False
        rewritten, changed = rewrite_note_tags(raw, mapping)
        if not changed:
            return False
        await asyncio.to_thread(self._write, vault_path, rewritten)
        return True

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="tags consolidate failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close tags-consolidate agent_runs row %s", run_id)

    # --- vault filesystem (atomic, ADR-014; mirrors the graph renderer) ----------------------

    def _read(self, vault_path: str) -> str | None:
        path = self._vault_root / Path(*vault_path.split("/"))
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _write(self, vault_path: str, contents: str) -> None:
        path = self._vault_root / Path(*vault_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, path)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Await any in-flight background apply (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
