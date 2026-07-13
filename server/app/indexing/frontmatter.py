"""Frontmatter parsing + node metadata extraction (02-data-model §2, §3).

Pure, no I/O — unit-tested with no mocks. The indexer needs a node's frontmatter *parsed*
(id / type / plane / planes / tags / aliases / disambig / occurred / edges / merged_into / …) to
fill the ``nodes`` row and materialize canonical edges, whereas the chunker only needs it
*stripped*. Both read the same ``split_frontmatter`` boundary.

We deliberately do **not** pull in a YAML dependency. Pipeline-written nodes use the small,
controlled shape rendered by ``graph/node_writer.py`` (``key: scalar``, ``key: [inline, list]``,
and an ``edges:`` block of ``- {rel: …, to: …}`` inline mappings), and user-authored nodes are
read leniently: anything this parser can't understand is ignored and the field falls back
(type ← folder, planes ← ``[plane]``, id ← a deterministic uuid5 of the store path, created ←
file mtime). A pipeline node's DB identity is its frontmatter ``id``; the path is a projection.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .chunking import split_frontmatter

# An ATX H1 (`# Title`) — the node's title when present (02 §2). Skips deeper headings.
_H1_RE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
# A fenced code-block delimiter — an H1 inside a fence is not a title.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# Namespace for the deterministic fallback id of a node file that carries no frontmatter ``id``
# (hand-authored nodes may omit any field, 02 §2). Stable across reindexes for a given path.
_ID_NAMESPACE = uuid.UUID("6f6e6f64-6500-4000-8000-000000000000")

# The default node type when a file declares none (02 §2: "Missing type = memory").
_DEFAULT_TYPE = "memory"


@dataclass(frozen=True)
class ParsedEdge:
    """A canonical edge parsed from a node's ``edges:`` frontmatter (02 §2, ADR-030/031/032)."""

    rel: str
    to: str  # target node id
    conf: float | None = None
    since: date | None = None
    until: date | None = None


@dataclass(frozen=True)
class NodeMetadata:
    """Frontmatter-derived fields for a ``nodes`` row + its canonical edges (02 §3). All optional
    at read time; ``id``/``type`` always resolve (fallbacks below)."""

    id: str
    type: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    aliases: list[str]
    disambig: str | None
    occurred_start: date | None
    occurred_end: date | None
    organizer_version: str | None
    merged_into: str | None
    source: str | None
    source_ref: str | None
    created: datetime | None
    edges: list[ParsedEdge] = field(default_factory=list)


def parse_frontmatter(inner: str) -> dict[str, str | list[str]]:
    """Parse the YAML *inner* block into a flat ``{key: scalar | list}`` mapping.

    Understands only the scalar/inline-list shapes this project emits; ``edges:`` block lists are
    parsed separately by :func:`parse_edges` (they are nested mappings this flat map can't hold).
    Comment / blank lines and anything more exotic are skipped rather than guessed at.
    """
    result: dict[str, str | list[str]] = {}
    for line in inner.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Only treat a colon as the key separator when the line starts at column 0 (a top-level
        # key), so a colon inside a value — or an indented ``edges:`` list item — doesn't split it.
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            result[key] = _parse_inline_list(raw[1:-1])
        else:
            result[key] = _unquote(raw)
    return result


def parse_edges(inner: str) -> list[ParsedEdge]:
    """Parse the ``edges:`` block — a run of ``- {rel: …, to: …, conf?, since?, until?}`` inline
    mappings — from the frontmatter inner text (02 §2). Malformed items (no ``rel``/``to``) are
    skipped rather than guessed at."""
    edges: list[ParsedEdge] = []
    in_block = False
    for line in inner.split("\n"):
        stripped = line.strip()
        if not in_block:
            if stripped == "edges:" or stripped.startswith("edges:"):
                # `edges:` opens the block; anything after the colon on the same line is not our
                # shape (we only emit the block form), so just enter block mode.
                in_block = True
            continue
        # Inside the block: list items are indented `- {…}`. Any non-indented line ends it.
        if line[:1] not in (" ", "\t"):
            break
        if not stripped.startswith("-"):
            continue
        item = stripped[1:].strip()
        if item.startswith("{") and item.endswith("}"):
            edge = _parse_edge_mapping(item[1:-1])
            if edge is not None:
                edges.append(edge)
    return edges


def parse_node_metadata(
    raw_text: str, *, store_path: str, fallback_created: datetime
) -> NodeMetadata:
    """Extract the ``nodes``-row metadata + canonical edges from a full node file (02 §2/§3).

    ``type`` falls back to the node's top-level folder (folder = type, 02 §1) then ``memory``;
    ``id`` to a deterministic uuid5 of the store path (hand-authored nodes without one stay stable
    across reindexes); ``title`` to the H1 (else the filename stem); ``created`` to the file mtime.
    ``plane`` does NOT fall back to the folder (the folder is the type now — planes are attributes).
    """
    inner, body = split_frontmatter(raw_text)
    fields = parse_frontmatter(inner) if inner is not None else {}
    edges = parse_edges(inner) if inner is not None else []

    node_id = _as_scalar(fields.get("id")) or _fallback_id(store_path)
    node_type = _as_scalar(fields.get("type")) or _folder_of(store_path) or _DEFAULT_TYPE
    plane = _as_scalar(fields.get("plane"))
    planes = _as_list(fields.get("planes"))
    if not planes and plane:
        planes = [plane]
    occurred_start, occurred_end = _expand_occurred(
        _as_scalar(fields.get("occurred")), _as_scalar(fields.get("occurred_end"))
    )
    title = _first_h1(body) or _stem_of(store_path)
    created = _parse_created(_as_scalar(fields.get("created"))) or fallback_created

    return NodeMetadata(
        id=node_id,
        type=node_type,
        title=title,
        plane=plane,
        planes=planes,
        tags=_as_list(fields.get("tags")),
        aliases=_as_list(fields.get("aliases")),
        disambig=_as_scalar(fields.get("disambig")),
        occurred_start=occurred_start,
        occurred_end=occurred_end,
        organizer_version=_as_scalar(fields.get("organizer_version")),
        merged_into=_as_scalar(fields.get("merged_into")),
        source=_as_scalar(fields.get("source")),
        source_ref=_as_scalar(fields.get("source_ref")),
        created=created,
        edges=edges,
    )


def _parse_edge_mapping(inner: str) -> ParsedEdge | None:
    """Parse the body of a ``{rel: …, to: …, …}`` inline mapping into a :class:`ParsedEdge`."""
    fields: dict[str, str] = {}
    for token in _split_top_level(inner, ","):
        key, sep, value = token.partition(":")
        if not sep:
            continue
        fields[key.strip()] = _unquote(value.strip())
    rel = fields.get("rel")
    to = fields.get("to")
    if not rel or not to:
        return None
    return ParsedEdge(
        rel=rel,
        to=to,
        conf=_parse_float(fields.get("conf")),
        since=_parse_date(fields.get("since")),
        until=_parse_date(fields.get("until")),
    )


def _parse_inline_list(inner: str) -> list[str]:
    """Split a ``a, "b, c", d`` inline-list body, honouring double quotes so a quoted value's
    comma doesn't split it."""
    return [_unquote(tok) for tok in _split_top_level(inner, ",") if _unquote(tok)]


def _split_top_level(inner: str, sep: str) -> list[str]:
    """Split on ``sep`` at the top level, ignoring separators inside double quotes."""
    items: list[str] = []
    current: list[str] = []
    in_quote = False
    escaped = False
    for ch in inner:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and in_quote:
            current.append(ch)
            escaped = True
        elif ch == '"':
            in_quote = not in_quote
            current.append(ch)
        elif ch == sep and not in_quote:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _unquote(value: str) -> str:
    """Strip surrounding double quotes and undo the renderer's ``\\"`` / ``\\\\`` escaping."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _as_scalar(value: str | list[str] | None) -> str | None:
    if isinstance(value, str):
        return value or None
    return None


def _as_list(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if v]
    if isinstance(value, str) and value:
        return [value]
    return []


def _first_h1(body: str) -> str | None:
    """First ATX H1 in the body, skipping any inside a fenced code block."""
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _H1_RE.match(line)
        if match:
            return match.group("title")
    return None


def _folder_of(store_path: str) -> str | None:
    """Top-level folder of a ``/``-separated store path = its node type (02 §1)."""
    head, _, tail = store_path.partition("/")
    return head if tail else None


def _stem_of(store_path: str) -> str:
    name = store_path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _fallback_id(store_path: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, store_path))


def _parse_created(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    """Parse a single-day ISO date (an edge ``since``/``until``). Partial values expand to their
    start day."""
    start, _ = _expand_occurred(value, None)
    return start


def _expand_occurred(
    occurred: str | None, occurred_end: str | None
) -> tuple[date | None, date | None]:
    """Expand a partial-ISO ``occurred`` (``2025`` | ``2025-07`` | ``2025-07-10``) to a
    ``[start, end]`` day range (02 §2, ADR-031 §2); an explicit ``occurred_end`` overrides the end
    with its own end-of-precision. Precision is implicit in the partial date. Unparseable ⇒ None."""
    parsed = _partial_range(occurred)
    if parsed is None:
        return None, None
    start, end = parsed
    if occurred_end:
        end_range = _partial_range(occurred_end)
        if end_range is not None:
            end = end_range[1]
    return start, end


def _partial_range(value: str | None) -> tuple[date, date] | None:
    """A partial-ISO string → ``(first_day, last_day)`` of its precision."""
    if not value:
        return None
    parts = value.strip().split("-")
    try:
        if len(parts) == 1:
            year = int(parts[0])
            return date(year, 1, 1), date(year, 12, 31)
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            return date(year, month, 1), date(year, month, _last_day(year, month))
        if len(parts) == 3:
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            return d, d
    except ValueError:
        return None
    return None


def _last_day(year: int, month: int) -> int:
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (first_next - timedelta(days=1)).day
