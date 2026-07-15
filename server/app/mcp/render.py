"""LLM-optimized Markdown serializers for the MCP tool results (M5 task 4, ADR-046 §3).

Pure functions over the service-layer DTOs — no business logic, no I/O — so they unit-test in
isolation. Chosen over JSON for token efficiency + native LLM parsing + cross-model fit (Claude
**and** GPT). IDs are rendered verbatim and labeled (each node carries an `id:` line) so the model
chains into `get_node`/`traverse`/`build_context`/`capture` with no precision loss. Hub edge lists
are capped inline with a `traverse` overflow pointer so one node never dumps hundreds of edges.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..graph.service import ContextNeighbor, NeighborPage, NodeContext
from ..graph.store import NeighborEdge
from ..search.service import NodePreview
from ..search.store import NodeEdgeView, SearchHit

_ARROWS = {"out": "→", "in": "←"}


def _arrow(direction: str) -> str:
    return _ARROWS.get(direction, "—")


def _title(title: str | None) -> str:
    return title if title and title.strip() else "(untitled)"


def _meta(type_: str, plane: str | None) -> str:
    return f"{type_}, {plane}" if plane else type_


def render_search_results(query: str, hits: list[SearchHit]) -> str:
    if not hits:
        return f'No nodes found for "{query}". This may not be in your memories yet.'
    lines = [f'# Search results for "{query}" ({len(hits)})', ""]
    for hit in hits:
        score = f"{hit.score:.3f}"
        lines.append(f"- **{_title(hit.title)}** ({_meta(hit.type, hit.plane)}) · score {score}")
        lines.append(f"  - id: `{hit.node_id}`")
        if hit.tags:
            lines.append(f"  - tags: {', '.join(hit.tags)}")
        if hit.snippet:
            lines.append(f"  - {hit.snippet.strip()}")
    lines.append("")
    lines.append("Use `get_node(id)` or `build_context(id)` to go deeper.")
    return "\n".join(lines)


def _edge_line(edge: NeighborEdge | NodeEdgeView, indent: str = "") -> str:
    plane = getattr(edge, "plane", None)
    return (
        f"{indent}- {_arrow(edge.dir)} `{edge.rel}` **{_title(edge.title)}** "
        f"({_meta(edge.type, plane)}) — id: `{edge.node_id}`"
    )


def _render_edges(
    edges: list[NodeEdgeView], node_id: str, cap: int, indent: str = ""
) -> list[str]:
    lines = [_edge_line(e, indent) for e in edges[:cap]]
    if len(edges) > cap:
        extra = len(edges) - cap
        lines.append(
            f'{indent}- …{extra} more edge(s); use `traverse(id="{node_id}")` to page the rest'
        )
    return lines


def render_node(node: NodePreview, *, edge_cap: int) -> str:
    if node.merged_into is not None:
        return (
            f"Node `{node.node_id}` was merged into `{node.merged_into}` — "
            f"use `get_node(id=\"{node.merged_into}\")`."
        )
    lines = [f"# {_title(node.title)}", ""]
    lines.append(f"- id: `{node.node_id}`")
    lines.append(f"- type: {node.type}")
    if node.planes:
        lines.append(f"- planes: {', '.join(node.planes)}")
    elif node.plane:
        lines.append(f"- plane: {node.plane}")
    if node.aliases:
        lines.append(f"- aliases: {', '.join(node.aliases)}")
    if node.tags:
        lines.append(f"- tags: {', '.join(node.tags)}")
    if node.occurred:
        span = str(node.occurred) + (f" – {node.occurred_end}" if node.occurred_end else "")
        lines.append(f"- occurred: {span}")
    if node.profile:
        lines += ["", "## Profile", node.profile.strip()]
    if node.body and node.body.strip():
        lines += ["", "## Content", node.body.strip()]
    if node.edges:
        lines += ["", f"## Edges ({len(node.edges)})"]
        lines += _render_edges(node.edges, node.node_id, edge_cap)
    return "\n".join(lines)


def render_traverse(page: NeighborPage) -> str:
    scope = f"`{page.rel}` " if page.rel else ""
    header = f"# Neighbors of `{page.center_id}` ({scope}{page.direction})"
    if not page.neighbors:
        return header + "\n\nNo matching neighbors."
    lines = [header, ""]
    lines += [_edge_line(e) for e in page.neighbors]
    if page.next_cursor:
        lines += [
            "",
            f'More available — `traverse(id="{page.center_id}", cursor="{page.next_cursor}")`.',
        ]
    return "\n".join(lines)


def _render_context_tree(
    neighbors: Iterable[ContextNeighbor], depth: int, edge_cap: int
) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    for cn in neighbors:
        lines.append(_edge_line(cn.edge, indent))
        if cn.neighbors:
            lines += _render_context_tree(cn.neighbors, depth + 1, edge_cap)
        if cn.truncated:
            lines.append(
                f'{indent}  - …more; use `traverse(id="{cn.edge.node_id}")` for the rest'
            )
    return lines


def render_build_context(ctx: NodeContext, *, edge_cap: int) -> str:
    parts: list[str] = []
    if ctx.identity_capsule and ctx.identity_capsule.strip():
        parts += ["## About the user (identity capsule)", ctx.identity_capsule.strip(), ""]
    parts.append(render_node(ctx.node, edge_cap=edge_cap))
    if ctx.neighbors:
        parts += ["", f"## Context (depth {ctx.depth})"]
        parts += _render_context_tree(ctx.neighbors, 0, edge_cap)
        if ctx.truncated:
            parts.append(
                f'- …the center has more neighbors; use `traverse(id="{ctx.node.node_id}")`'
            )
    return "\n".join(parts)


def render_planes(planes: list[str], inbox: str) -> str:
    lines = ["# Planes", ""] + [f"- {p}" for p in planes]
    lines += ["", f"Unclassifiable captures land in `{inbox}`."]
    return "\n".join(lines)


def render_types(node_types: list[str], edge_rels: list[str], entity_types: list[str]) -> str:
    return "\n".join(
        [
            "# Vocabulary",
            "",
            f"- **node types**: {', '.join(node_types)}",
            f"- **edge relations**: {', '.join(edge_rels)}",
            f"- **entity (hub) types**: {', '.join(entity_types)}",
        ]
    )


def render_capture_ack(capture_id: str) -> str:
    return (
        "Capture accepted and queued for organizing into your graph.\n\n"
        f"- capture id: `{capture_id}`\n\n"
        "It is processed in the background (a few seconds). Use `search` shortly to confirm the "
        "resulting node(s) — the organizer decides the node type, plane, and edges."
    )


def render_identity_capsule(text: str | None) -> str:
    if not text or not text.strip():
        return "No identity capsule has been generated yet."
    return "# About the user\n\n" + text.strip()
