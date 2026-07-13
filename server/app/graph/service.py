"""Relatedness-graph recompute (ADR-023).

One entry point, :meth:`RelatednessGraph.recompute`, does the whole wholesale rebuild:

    top-K over notes.embedding cosine above RELATED_MIN_SCORE
      → materialize note_links (canonical, directional)
      → render the sb:related block into each note body (churn-gated)
      → request a vault commit if any file changed

The graph is **global** (adding one note can shift others' neighbours), so it is recomputed as a
whole — nightly, and on ``POST /admin/reindex`` (wired by the reindex task) — never on the
real-time capture write. Churn control (a note file is rewritten only when its content actually
changes) keeps a stable graph at **zero** nightly git writes; the indexer's ``content_hash``
excludes the ``sb:related`` block so these writes never re-trigger a reindex (the feedback-loop
fix, ADR-023).

Git is not this class's concern (as with :class:`~app.capture.notes.NoteWriter`): it writes files
and asks the :class:`~app.services.vault_backup.VaultBackup` seam to (eventually) commit + push
them under the single vault lock. A no-change run touches neither disk nor git.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..services.vault_backup import VaultBackup
from .renderer import apply_related_block
from .store import GraphStore, RelatedLink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphOutcome:
    """Result of a graph recompute (feeds the ``reindex`` agent_runs details later)."""

    notes: int = 0  # indexed notes considered by the render pass
    links: int = 0  # note_links edge rows written
    blocks_written: int = 0  # note files whose sb:related block actually changed
    failed: int = 0  # notes whose block could not be rendered (skipped, run continued)
    commit_requested: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "notes": self.notes,
            "links": self.links,
            "blocks_written": self.blocks_written,
            "failed": self.failed,
            "commit_requested": self.commit_requested,
        }


class RelatednessGraph:
    """Recomputes ``note_links`` + renders the ``sb:related`` vault blocks (ADR-023)."""

    def __init__(
        self, *, settings: Settings, store: GraphStore, vault_backup: VaultBackup
    ) -> None:
        self._settings = settings
        self._store = store
        self._backup = vault_backup
        self._vault_root = Path(settings.vault_path)
        self._top_k = settings.related_top_k
        self._min_score = settings.related_min_score

    async def recompute(self) -> GraphOutcome:
        """Full wholesale recompute of the graph + rendered blocks. Never partial (ADR-023)."""
        neighbors = await self._store.compute_neighbors(
            top_k=self._top_k, min_score=self._min_score
        )
        links = await self._store.replace_note_links(neighbors)

        by_path = {n.vault_path: n.related for n in neighbors}
        all_paths = await self._store.list_note_paths()
        changed = failed = 0
        for vault_path in all_paths:
            try:
                if await self._render_one(vault_path, by_path.get(vault_path, [])):
                    changed += 1
            except Exception:  # noqa: BLE001 — one bad note must not abort the render (rule 7)
                logger.exception("relatedness: failed to render %s, skipping", vault_path)
                failed += 1

        commit_requested = False
        if changed:
            # Fire-and-forget through the single git owner; the nightly reindex job / the debounce
            # window folds this into one commit+push under the vault lock (ADR-014/023).
            await self._backup.request_commit(f"relatedness: {changed} note(s) updated")
            commit_requested = True

        logger.info(
            "relatedness recompute: %d notes, %d links, %d block(s) rewritten, %d failed",
            len(all_paths),
            links,
            changed,
            failed,
        )
        return GraphOutcome(
            notes=len(all_paths),
            links=links,
            blocks_written=changed,
            failed=failed,
            commit_requested=commit_requested,
        )

    async def _render_one(self, vault_path: str, related: list[RelatedLink]) -> bool:
        """Render a note's ``sb:related`` block; write only if the file content changed (churn
        gate). Returns True when the file was rewritten. A missing file is skipped (a rescan
        reconciles a deleted note's row separately); any other read/write error propagates to the
        loop, which logs it and continues with the next note (skip-and-continue, rule 7)."""
        raw = await asyncio.to_thread(self._read, vault_path)
        if raw is None:
            logger.warning("relatedness: note %s missing on disk, skipping block", vault_path)
            return False
        rendered = apply_related_block(raw, related)
        if rendered == raw:
            return False
        await asyncio.to_thread(self._write, vault_path, rendered)
        return True

    def _read(self, vault_path: str) -> str | None:
        """Read a note's text (``None`` if the file is gone). A present-but-unreadable file
        (bad encoding, permissions) raises — the caller's loop skips it and continues (rule 7)."""
        path = self._vault_root / Path(*vault_path.split("/"))
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _write(self, vault_path: str, contents: str) -> None:
        """Atomic overwrite (temp + ``os.replace``, ADR-014) — a note is never left half-written."""
        path = self._vault_root / Path(*vault_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, path)
