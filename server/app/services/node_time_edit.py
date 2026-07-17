"""The mechanical token-edit tier of ADR-056 §5 (M8.2 Task 3-F).

Every rendered date in a node body is a tap-to-edit target; the ``[[t:…]]`` token **is** the edit
anchor (no text-span bookkeeping). A **token edit** rewrites that token to a new date and — when the
token is the node's *event* date — updates ``occurred``/``occurred_end`` to match, then re-embeds
the node. It is purely mechanical: no LLM, instant (the user supplies the new date; the resolver
already ran at ingest). The **anchor edit** (correcting the capture's recorded-at → a background
reorganize) is the capture pipeline's job, not this service's.

Thin over the store (rule 5): reads the node's current state via the search service (store_path +
the event ``occurred`` to decide whether this token is the event date), writes via the shared
:class:`~app.graph.node_writer.NodeWriter`, then re-indexes so the vectors + ``occurred_*`` columns
follow the edited file (rule 1 — the store is truth, the DB is derived).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..graph.node_writer import NodeWriter
from ..indexing.indexer import NodeIndexer
from ..search.service import SearchService
from ..temporal.tokens import TOKEN_RE, PartialDate, ResolvedTime, parse_inner
from .store_backup import StoreBackup

logger = logging.getLogger(__name__)


class NodeTimeEditError(Exception):
    """Base for token-edit problems surfaced to the API layer."""


class NodeNotFound(NodeTimeEditError):
    """No live (non-tombstone) node with the given id (404)."""


class BadTimeEdit(NodeTimeEditError):
    """The token/date payload is invalid, or the token isn't in the node body (400)."""


@dataclass(frozen=True)
class TimeEditResult:
    """The outcome of a token edit — what the endpoint returns."""

    node_id: str
    occurred_updated: bool
    occurred: str | None
    occurred_end: str | None


class NodeTimeEditService:
    def __init__(
        self,
        *,
        search_service: SearchService,
        node_writer: NodeWriter,
        indexer: NodeIndexer,
        store_backup: StoreBackup,
    ) -> None:
        self._search = search_service
        self._writer = node_writer
        self._indexer = indexer
        self._backup = store_backup

    async def edit_token(
        self,
        node_id: str,
        *,
        old_token: str,
        start: str,
        end: str | None = None,
        label: str | None = None,
    ) -> TimeEditResult:
        """Rewrite ``old_token`` (an exact ``[[t:…]]`` string in the body) to a new date built from
        ``start``/``end``/``label`` (partial-ISO). If the old token is the node's event date,
        ``occurred``/``occurred_end`` are updated too; the node's chunks are re-embedded. Raises
        :class:`NodeNotFound` (404) / :class:`BadTimeEdit` (400)."""
        old_rt = _parse_token(old_token)
        if old_rt is None:
            raise BadTimeEdit("`old` is not a valid [[t:…]] date token")
        new_rt = _build_resolved(start, end, label)

        preview = await self._search.get_node(node_id)
        if preview is None or preview.merged_into:
            raise NodeNotFound(node_id)

        # Is this token the node's event date? Compare the day-granular occurred span (dates) — the
        # only mechanically-knowable link between a body token and `occurred` (ADR-056 §5). The DB
        # `occurred_end` is NEVER null for a dated node: the indexer collapses a day-precise
        # `occurred` to `occurred_end == occurred_start` (frontmatter `_expand_occurred`), whereas
        # `ResolvedTime.occurred_end()` returns None for a precise point — so fall back to the start
        # to compare like-for-like (else a day-precise event date never matches, the common case).
        old_end = old_rt.occurred_end() or old_rt.occurred_start()
        is_event = (
            preview.occurred is not None
            and preview.occurred == old_rt.occurred_start()
            and preview.occurred_end == old_end
        )
        new_occurred = new_rt.start_date_iso()
        new_occurred_end = new_rt.end_date_iso()
        try:
            replaced = await asyncio.to_thread(
                self._writer.edit_time_token,
                preview.store_path,
                old_token=old_token,
                new_token=new_rt.token(),
                occurred=new_occurred,
                occurred_end=new_occurred_end,
                update_occurred=is_event,
            )
        except FileNotFoundError:
            raise NodeNotFound(node_id) from None
        if replaced == 0:
            raise BadTimeEdit("the token was not found in the node body")

        await self._indexer.index_paths([preview.store_path])
        await self._backup.request_commit("edit: date token")
        return TimeEditResult(
            node_id=node_id,
            occurred_updated=is_event,
            occurred=new_occurred if is_event else None,
            occurred_end=new_occurred_end if is_event else None,
        )


def _parse_token(token: str) -> ResolvedTime | None:
    """Parse a full ``[[t:…]]`` token string into a :class:`ResolvedTime`, or ``None`` if it is not
    a well-formed token."""
    match = TOKEN_RE.fullmatch(token.strip())
    if match is None:
        return None
    return parse_inner(match.group(1))


def _build_resolved(start: str, end: str | None, label: str | None) -> ResolvedTime:
    """Build the new :class:`ResolvedTime` from the request's partial-ISO fields; raises
    :class:`BadTimeEdit` on any malformed/impossible value (fail-closed — never a guessed date)."""
    start_pd = PartialDate.parse(start)
    if start_pd is None:
        raise BadTimeEdit(f"`start` is not a valid partial-ISO date: {start!r}")
    end_pd: PartialDate | None = None
    if end:
        end_pd = PartialDate.parse(end)
        if end_pd is None:
            raise BadTimeEdit(f"`end` is not a valid partial-ISO date: {end!r}")
    clean_label = (label.strip() if label else None) or None
    return ResolvedTime(start=start_pd, end=end_pd, label=clean_label)
