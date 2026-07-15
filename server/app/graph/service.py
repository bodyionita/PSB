"""Graph services — the derived-edge recompute (write side) and the read primitives MCP + the
M7 map consume (M5 task 1, [ADR-046](adr/046-m5-mcp-server-oauth-connectors.md) / ADR-028 / 032).

**:class:`DerivedEdgeGraph`** (ADR-023 surviving half, retargeted at M3) — one entry point,
:meth:`~DerivedEdgeGraph.recompute`, does the whole wholesale rebuild::

    top-K over nodes.embedding cosine above SIMILAR_MIN_SCORE
      → replace edges(origin='derived', rel='similar')   [DB-only]

The graph is **global** (adding one node can shift others' neighbours), so it is recomputed as a
whole — nightly, and on ``POST /admin/reindex`` (wired by the reindex task) — never on the
real-time capture write. Unlike the pre-pivot relatedness graph, this is **DB-only**: there is no
file rendering, no ``sb:related`` block, no commit step, no churn-gating (all deleted by
[ADR-026](adr/026-graph-native-storage-obsidian-removed.md)). Canonical edges are materialized
separately, from frontmatter, by the indexer.

**:class:`GraphService`** — the read side. :meth:`~GraphService.neighbors` is the cursor-paginated
one-hop primitive behind MCP ``traverse`` + ``GET /nodes/{id}/neighbors`` (M7 reuses it);
:meth:`~GraphService.build_context` bundles ``get_node`` + a bounded neighbor tree (depth ≤ 2,
fanout-capped) for one MCP round-trip. Both are **thin over the store** — no LLM call — and return
structured values; the Markdown rendering + the L0 identity capsule are the MCP boundary's job
(task 4 / task 2). All results respect the finite-context caps in :class:`~app.config.Settings`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from ..config import Settings
from ..identity.store import IdentityCapsuleReader
from ..search.service import NodePreview
from .store import GraphStore, NeighborCursor, NeighborEdge, NeighborStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphOutcome:
    """Result of a derived-edge recompute (feeds the ``reindex`` agent_runs details later)."""

    edges: int = 0  # derived `similar` edge rows written

    def as_dict(self) -> dict[str, object]:
        return {"edges": self.edges}


class DerivedEdgeGraph:
    """Recomputes the ``edges(origin='derived')`` neighbour set (ADR-023 surviving half)."""

    def __init__(self, *, settings: Settings, store: GraphStore) -> None:
        self._store = store
        self._top_k = settings.similar_top_k
        self._min_score = settings.similar_min_score

    async def recompute(self) -> GraphOutcome:
        """Full wholesale recompute of the derived edges. Never partial (ADR-023). DB-only."""
        edges = await self._store.compute_similar(top_k=self._top_k, min_score=self._min_score)
        written = await self._store.replace_derived_edges(edges)
        logger.info("derived-edge recompute: %d similar edge(s) written", written)
        return GraphOutcome(edges=written)


class InvalidDirection(ValueError):
    """A ``neighbors`` direction that isn't ``out``/``in``/``both`` (→ 422 / MCP tool error)."""


class InvalidCursor(ValueError):
    """A pagination cursor that doesn't decode — tampered, truncated, or from another shape."""


class NodeReader(Protocol):
    """The node-detail read ``build_context`` needs — the ``get_node`` subset of ``SearchService``.

    Depending on the narrow protocol (not the whole service) keeps ``GraphService`` unit-testable
    with a fake and mirrors the chat service's ``Retriever`` seam (rule 10)."""

    async def get_node(self, node_id: str) -> NodePreview | None: ...


@dataclass(frozen=True)
class NeighborPage:
    """One cursor-paginated slice of a node's 1-hop neighbors (MCP ``traverse`` / M7 map).

    ``next_cursor`` is an opaque token to pass back for the following page, or ``None`` when the
    neighborhood is exhausted. ``rel``/``direction`` echo the filters this page was built with."""

    center_id: str
    neighbors: list[NeighborEdge]
    next_cursor: str | None
    rel: str | None
    direction: str


@dataclass(frozen=True)
class ContextNeighbor:
    """A neighbor inside a ``build_context`` bundle: the edge + endpoint, plus its own (capped)
    neighbors when the traversal goes deeper. ``truncated`` marks that this node has more 1-hop
    neighbors than the fanout cap surfaced — reach the rest with ``traverse``."""

    edge: NeighborEdge
    neighbors: list[ContextNeighbor] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class NodeContext:
    """The ``build_context`` result: the identity capsule (L0), the center node, its neighbor tree.

    ``identity_capsule`` is the last-distilled capsule text (ADR-046 §5, served as L0), or ``None``
    when no capsule exists yet / the read failed (never generated inline — rule 7). ``depth`` is the
    effective (clamped) traversal depth; ``truncated`` marks the center having more 1-hop neighbors
    than the fanout cap included."""

    node: NodePreview
    neighbors: list[ContextNeighbor]
    depth: int
    truncated: bool
    identity_capsule: str | None = None


class GraphService:
    """One-hop traversal + bounded context assembly over the graph (M5 task 1). Thin over the
    store — no LLM call — so MCP ``traverse``/``build_context`` and the M7 map share one seam. The
    ``build_context`` L0 identity capsule is a cheap ``app_settings`` read (task 2), never distilled
    inline."""

    _DIRECTIONS = frozenset({"out", "in", "both"})

    def __init__(
        self,
        *,
        settings: Settings,
        store: NeighborStore,
        nodes: NodeReader,
        capsule: IdentityCapsuleReader | None = None,
    ) -> None:
        self._store = store
        self._nodes = nodes
        self._capsule = capsule
        self._page_default = settings.graph_neighbors_page_default
        self._page_max = settings.graph_neighbors_page_max
        self._depth_default = settings.build_context_default_depth
        self._depth_max = settings.build_context_max_depth
        self._fanout = settings.build_context_fanout

    async def neighbors(
        self,
        node_id: str,
        *,
        rel: str | None = None,
        direction: str = "both",
        cursor: str | None = None,
        limit: int | None = None,
    ) -> NeighborPage:
        """One page of ``node_id``'s 1-hop neighbors (03-api §MCP ``traverse`` / §Nodes neighbors).

        ``rel`` filters to one relation; ``direction`` is ``out``/``in``/``both``; ``cursor``
        resumes a prior page; ``limit`` is clamped to the configured page ceiling. Raises
        :class:`InvalidDirection` / :class:`InvalidCursor` on bad input. An unknown or isolated
        node yields an empty page (existence is the caller's concern — build_context / router)."""
        if direction not in self._DIRECTIONS:
            raise InvalidDirection(direction)
        after = _decode_cursor(cursor) if cursor is not None else None
        page_size = self._clamp_limit(limit)
        # Fetch one extra to learn whether a further page exists without a second round-trip.
        rows = await self._store.neighbors(
            node_id,
            rel=rel or None,
            direction=None if direction == "both" else direction,
            after=after,
            limit=page_size + 1,
        )
        page = rows[:page_size]
        has_more = len(rows) > page_size
        next_cursor = _encode_cursor(page[-1]) if has_more and page else None
        return NeighborPage(
            center_id=node_id,
            neighbors=page,
            next_cursor=next_cursor,
            rel=rel or None,
            direction=direction,
        )

    async def build_context(
        self, node_id: str, *, depth: int | None = None
    ) -> NodeContext | None:
        """``get_node`` + a bounded neighbor tree in one call (03-api §MCP ``build_context``).

        Returns ``None`` if the node is unknown. ``depth`` is clamped to
        ``[0, build_context_max_depth]`` (default max 2 = the 03-api ``depth ≤ 2`` contract,
        ADR-032); every visited node contributes at most ``build_context_fanout`` neighbors (the
        rest flagged truncated). The L0 identity capsule is layered in by M5 task 2."""
        center = await self._nodes.get_node(node_id)
        if center is None:
            return None
        capsule = await self._identity_capsule()
        effective_depth = self._clamp_depth(depth)
        if effective_depth <= 0:
            return NodeContext(
                node=center, neighbors=[], depth=effective_depth, truncated=False,
                identity_capsule=capsule,
            )
        tree, truncated = await self._expand(node_id, effective_depth, {node_id})
        return NodeContext(
            node=center, neighbors=tree, depth=effective_depth, truncated=truncated,
            identity_capsule=capsule,
        )

    async def _identity_capsule(self) -> str | None:
        """The last-distilled capsule text served as L0 (ADR-046 §5) — a cheap ``app_settings``
        read, omitted if absent and never generated inline. Best-effort: a read failure yields
        ``None`` rather than failing the whole bundle (rule 7)."""
        if self._capsule is None:
            return None
        try:
            blob = await self._capsule.current()
        except Exception:  # noqa: BLE001 — the capsule is grounding, not load-bearing for a bundle
            logger.warning("build_context: capsule read failed; omitting L0", exc_info=True)
            return None
        return blob.text if blob else None

    async def _expand(
        self, node_id: str, remaining: int, seen: frozenset[str] | set[str]
    ) -> tuple[list[ContextNeighbor], bool]:
        """The center's (or a subtree's) capped 1-hop neighbors, recursing while ``remaining`` > 1.

        ``seen`` (the ids already on the path) stops a two-cycle (A↔B) from re-expanding forever;
        a node already seen is still listed, just not expanded again. Returns the neighbor list and
        whether this level was fanout-truncated."""
        page = await self.neighbors(node_id, limit=self._fanout)
        children: list[ContextNeighbor] = []
        for edge in page.neighbors:
            sub: list[ContextNeighbor] = []
            sub_truncated = False
            if remaining > 1 and edge.node_id not in seen:
                sub, sub_truncated = await self._expand(
                    edge.node_id, remaining - 1, seen | {edge.node_id}
                )
            children.append(
                ContextNeighbor(edge=edge, neighbors=sub, truncated=sub_truncated)
            )
        return children, page.next_cursor is not None

    def _clamp_limit(self, limit: int | None) -> int:
        requested = limit if limit is not None else self._page_default
        return max(1, min(requested, self._page_max))

    def _clamp_depth(self, depth: int | None) -> int:
        requested = depth if depth is not None else self._depth_default
        return max(0, min(requested, self._depth_max))


def _encode_cursor(edge: NeighborEdge) -> str:
    """Opaque, URL-safe token carrying the keyset the next page resumes after (the four ORDER-BY
    columns of this edge). Base64 of compact JSON — clients treat it as a blob."""
    raw = json.dumps([edge.origin, edge.rel, edge.dir, edge.node_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> NeighborCursor:
    """Reverse of :func:`_encode_cursor`. Raises :class:`InvalidCursor` on anything that isn't a
    well-formed four-string keyset (tampered, truncated, or from an incompatible shape)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        parts = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise InvalidCursor(cursor) from exc
    if (
        not isinstance(parts, list)
        or len(parts) != 4
        or not all(isinstance(p, str) for p in parts)
    ):
        raise InvalidCursor(cursor)
    return (parts[0], parts[1], parts[2], parts[3])
