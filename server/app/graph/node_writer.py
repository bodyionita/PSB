"""Node rendering + graph-store writing (02-data-model §1/§2, ADR-026/030/031/032).

Two layers:
  * Pure helpers — slug/frontmatter/body rendering — unit-tested with no I/O.
  * :class:`NodeWriter` — the only place the organizer's nodes touch the filesystem. Writes are
    atomic (temp + ``os.replace``, ADR-014) and collision-safe (numeric suffix). Git is NOT this
    class's concern: it only writes/removes files; the ``StoreBackupService`` commits them.

The store layout (02 §1): **folder = node type** (``memory/``, ``person/``, …); the one system
folder ``inbox/`` holds organizer-fallback nodes. **Filename = ``<slug>--<shortid>.md``**, memory
nodes prefixed by the date for readability. Identity is the frontmatter ``id`` (a uuid) — the
filename is a rename-safe projection the indexer never keys on.

Store-relative paths are always ``/``-separated regardless of OS (CLAUDE.md conventions). Blocking
filesystem work is synchronous here; the pipeline calls it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..indexing.chunking import split_frontmatter

# The generation stamp written into every pipeline node's frontmatter (02 §2). Retrofit passes
# target "everything below vN" instead of re-walking the whole graph (ADR-031 §4). Bump on any
# change to the node contract the organizer emits.
ORGANIZER_VERSION = "v3"

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 80  # keep filenames comfortably under path limits


def slugify(title: str) -> str:
    """Filename-safe slug: lower-case, non-alphanumerics collapsed to ``-``, length-bounded.

    Never returns empty (falls back to ``"untitled"``). This is a *filename* slug — distinct from
    the stricter Obsidian *tag* slug (``organizer._slugify_tag``), which must also stay valid as a
    ``#tag``.
    """
    slug = _SLUG_INVALID.sub("-", title.strip().lower()).strip("-")
    slug = slug[:_MAX_SLUG_LEN].strip("-")
    return slug or "untitled"


def short_id(node_id: str) -> str:
    """The filename's short id: the first hex group of the uuid (02 §1 example ``018f3c2e``)."""
    return node_id.split("-", 1)[0]


def node_filename(*, node_id: str, node_type: str, title: str, created_local: datetime) -> str:
    """``<slug>--<shortid>.md``; ``memory`` nodes prefix the date (02 §1)."""
    stem = f"{slugify(title)}--{short_id(node_id)}"
    if node_type == "memory":
        return f"{created_local:%Y-%m-%d}--{stem}.md"
    return f"{stem}.md"


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when needed (special chars); escape embedded quotes."""
    if value and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


@dataclass(frozen=True)
class NodeEdge:
    """One canonical typed edge written on a node's frontmatter (02 §2, ADR-030/031/032).

    ``to`` is the **target node id** (a uuid); ``conf`` omitted ⇒ 1.0; ``since``/``until`` are the
    partial-ISO validity window (``until`` closes a superseded relation — invalidate, never delete).
    """

    rel: str
    to: str
    conf: float | None = None
    since: str | None = None
    until: str | None = None

    def render(self) -> str:
        """Inline-mapping form: ``{rel: involves, to: <id>, since: 2025-07-10}`` (02 §2)."""
        parts = [f"rel: {_yaml_scalar(self.rel)}", f"to: {_yaml_scalar(self.to)}"]
        if self.conf is not None:
            parts.append(f"conf: {self.conf:g}")
        if self.since:
            parts.append(f"since: {_yaml_scalar(self.since)}")
        if self.until:
            parts.append(f"until: {_yaml_scalar(self.until)}")
        return "{" + ", ".join(parts) + "}"


@dataclass(frozen=True)
class NodeDocument:
    """A fully-resolved node ready to be written (the write-side contract the organizer fills).

    Every edge target is already a node id (existing entity or a freshly-minted sibling) — the
    writer never resolves; it renders what it is given. ``in_inbox`` routes the never-lose fallback
    to ``inbox/`` instead of the ``type`` folder; ``aliases``/``disambig`` are entity-like only.
    """

    id: str
    type: str
    title: str
    body: str
    created_local: datetime
    source: str
    source_ref: str | None = None
    plane: str | None = None
    planes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    occurred: str | None = None
    occurred_end: str | None = None
    organizer_version: str = ORGANIZER_VERSION
    edges: tuple[NodeEdge, ...] = ()
    aliases: tuple[str, ...] = ()
    disambig: str | None = None
    in_inbox: bool = False

    @property
    def folder(self) -> str:
        """Top-level store folder: the type, or ``inbox/`` for the fallback node (02 §1)."""
        return "inbox" if self.in_inbox else self.type


def render_frontmatter(node: NodeDocument) -> str:
    """Render a node's YAML frontmatter block (02 §2). Optional fields are omitted when absent."""
    lines = [
        "---",
        f"id: {node.id}",
        f"type: {_yaml_scalar(node.type)}",
        f"created: {node.created_local.isoformat()}",
    ]
    if node.occurred:
        lines.append(f"occurred: {_yaml_scalar(node.occurred)}")
    if node.occurred_end:
        lines.append(f"occurred_end: {_yaml_scalar(node.occurred_end)}")
    lines.append(f"source: {_yaml_scalar(node.source)}")
    if node.source_ref:
        lines.append(f"source_ref: {_yaml_scalar(node.source_ref)}")
    if node.plane:
        lines.append(f"plane: {_yaml_scalar(node.plane)}")
    lines.append(f"planes: {_yaml_list(node.planes)}")
    lines.append(f"tags: {_yaml_list(node.tags)}")
    if node.aliases:
        lines.append(f"aliases: {_yaml_list(node.aliases)}")
    if node.disambig:
        lines.append(f"disambig: {_yaml_scalar(node.disambig)}")
    lines.append(f"organizer_version: {_yaml_scalar(node.organizer_version)}")
    if node.edges:
        lines.append("edges:")
        lines.extend(f"  - {edge.render()}" for edge in node.edges)
    lines.append("---")
    return "\n".join(lines)


def render_node(node: NodeDocument) -> str:
    """Full node file contents: frontmatter + H1 title + body (02 §2)."""
    return (
        "\n".join([render_frontmatter(node), "", f"# {node.title}", "", node.body.strip()]).rstrip()
        + "\n"
    )


def append_edges(raw_text: str, edges: tuple[NodeEdge, ...] | list[NodeEdge]) -> str:
    """Append canonical ``edges`` to an existing node file's frontmatter, leaving body untouched.

    Pure (no I/O) so it is unit-tested directly. Used to **materialize a pending entity edge on
    review resolution** (ADR-030 §3) — the organizer left the edge unwritten pending a human pick,
    and this writes it once the target is known. An edge already present (same ``rel`` + ``to``) is
    skipped, so re-materialization is idempotent (rule 6). Creates the ``edges:`` block if absent.

    The node contract renders ``edges:`` as the frontmatter's last block, but this inserts at the
    end of the existing block wherever it sits, so a hand-authored ordering is preserved too. Raises
    :class:`ValueError` if the file has no frontmatter (there is no contract node to edit).
    """
    inner, body = split_frontmatter(raw_text)
    if inner is None:
        raise ValueError("cannot append edges: node file has no frontmatter")
    lines = inner.rstrip("\n").split("\n")
    to_add = [e for e in edges if not _edge_present(lines, e)]
    if not to_add:
        return raw_text
    edges_idx = next((i for i, line in enumerate(lines) if line.strip() == "edges:"), None)
    if edges_idx is None:
        lines.append("edges:")
        insert_at = len(lines)
    else:
        # End of the block = first line after `edges:` that is not an indented list continuation.
        insert_at = edges_idx + 1
        while insert_at < len(lines) and lines[insert_at][:1] in (" ", "\t"):
            insert_at += 1
    for offset, edge in enumerate(to_add):
        lines.insert(insert_at + offset, f"  - {edge.render()}")
    return f"---\n{chr(10).join(lines)}\n---\n{body}"


def _edge_present(frontmatter_lines: list[str], edge: NodeEdge) -> bool:
    """True if an ``edges:`` item already links the same ``rel`` + ``to`` (ignoring dates)."""
    rel_token = f"rel: {_yaml_scalar(edge.rel)}"
    to_token = f"to: {_yaml_scalar(edge.to)}"
    return any(
        line.lstrip().startswith("-") and rel_token in line and to_token in line
        for line in frontmatter_lines
    )


def retarget_edges(raw_text: str, *, old_to: str, new_to: str) -> tuple[str, int]:
    """Rewrite every canonical edge ``to: old_to`` → ``to: new_to`` (a merge redirect, ADR-030 §5).

    Pure (no I/O) so it is unit-tested directly. Used when merging ``old_to`` into ``new_to``: each
    node with an inbound edge to the loser has that edge retargeted onto the survivor. If the
    retarget produces an edge identical (same ``rel`` + ``to``) to one already on the node, the
    duplicate item is dropped so the ``(src, dst, rel, origin)`` pk can't be violated on reindex.
    Returns ``(new_text, retargeted_count)``; a file with no matching edge is returned verbatim
    (no newline churn). Only the ``edges:`` block is touched — every other byte is preserved.
    """
    inner, body = split_frontmatter(raw_text)
    if inner is None:
        return raw_text, 0
    old_token = f"to: {_yaml_scalar(old_to)}"
    new_token = f"to: {_yaml_scalar(new_to)}"
    lines = inner.rstrip("\n").split("\n")
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    in_edges = False
    retargeted = 0
    for line in lines:
        stripped = line.strip()
        if not in_edges:
            out.append(line)
            if stripped == "edges:":
                in_edges = True
            continue
        if line[:1] not in (" ", "\t"):
            in_edges = False
            out.append(line)
            continue
        if not stripped.startswith("-"):
            out.append(line)
            continue
        if old_token in line:
            line = line.replace(old_token, new_token)
            retargeted += 1
        key = _edge_identity(line)
        if key is not None and key in seen:
            continue  # duplicate after retarget — drop it
        if key is not None:
            seen.add(key)
        out.append(line)
    if retargeted == 0:
        return raw_text, 0
    return f"---\n{chr(10).join(out)}\n---\n{body}", retargeted


def _edge_identity(edge_line: str) -> tuple[str, str] | None:
    """``(rel, to)`` of an ``- {rel: …, to: …}`` edge line, for dedup; ``None`` if unparseable."""
    item = edge_line.strip().lstrip("-").strip()
    if not (item.startswith("{") and item.endswith("}")):
        return None
    fields: dict[str, str] = {}
    for token in item[1:-1].split(","):
        key, sep, value = token.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    rel, to = fields.get("rel"), fields.get("to")
    return (rel, to) if rel and to else None


def upsert_frontmatter_list(raw_text: str, key: str, values: tuple[str, ...] | list[str]) -> str:
    """Set a top-level ``key: [inline, list]`` frontmatter line (replace if present, else insert).

    Pure. Used to union a merge survivor's ``aliases`` (ADR-030 §5). The line is inserted before
    the ``edges:`` block (or the closing ``---``) so a hand-authored ordering of the other keys is
    preserved. Raises :class:`ValueError` if the file has no frontmatter."""
    inner, body = split_frontmatter(raw_text)
    if inner is None:
        raise ValueError(f"cannot set {key}: node file has no frontmatter")
    rendered = f"{key}: {_yaml_list(values)}"
    lines = inner.rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        if line[:1] not in (" ", "\t") and line.partition(":")[0].strip() == key:
            lines[i] = rendered
            return f"---\n{chr(10).join(lines)}\n---\n{body}"
    insert_at = next((i for i, ln in enumerate(lines) if ln.strip() == "edges:"), len(lines))
    lines.insert(insert_at, rendered)
    return f"---\n{chr(10).join(lines)}\n---\n{body}"


def render_tombstone(*, node_id: str, node_type: str, survivor_id: str) -> str:
    """A merged node's replacement file: keeps only ``id``/``type``/``merged_into`` (02 §2, ADR-030
    §5). The id keeps resolving (old links + source_refs redirect to the survivor); the indexer
    reads ``merged_into`` and hides the node from search/map."""
    return "\n".join(
        [
            "---",
            f"id: {node_id}",
            f"type: {_yaml_scalar(node_type)}",
            f"merged_into: {_yaml_scalar(survivor_id)}",
            "---",
            "",
            "(merged)",
            "",
        ]
    )


def merged_alias_union(
    survivor_aliases: tuple[str, ...] | list[str],
    survivor_title: str | None,
    loser_aliases: tuple[str, ...] | list[str],
    loser_title: str | None,
) -> list[str]:
    """The survivor's aliases after a merge: its own aliases + the loser's name + aliases, deduped
    (ADR-030 §5 — the loser's surface forms keep resolving, now onto the survivor). The survivor's
    own title is included so the set is a complete alias list even if it lacked one."""
    return _unique(
        [
            *survivor_aliases,
            *( [survivor_title] if survivor_title else [] ),
            *( [loser_title] if loser_title else [] ),
            *loser_aliases,
        ]
    )


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return seen


@dataclass(frozen=True)
class WrittenNode:
    """The result of writing one node — its id and store-relative (``/``-separated) path."""

    node_id: str
    store_path: str


class NodeWriter:
    """Writes the organizer's nodes into the graph store. Atomic + collision-safe."""

    def __init__(self, graph_store_path: str) -> None:
        self._root = Path(graph_store_path)

    def _reserve_path(self, folder: str, filename: str, reserved: set[str]) -> tuple[Path, str]:
        """Resolve a non-colliding target, honouring both on-disk files and sibling reservations.

        Collisions are astronomically unlikely (the short id derives from a unique uuid) but a slug
        clash is still handled so a write never silently overwrites a different node's file — the
        indexer keys on the frontmatter ``id``, so the filename only needs to be unique on disk.
        """
        stem = filename[:-3]  # drop ".md"
        candidate = filename
        counter = 2
        while True:
            rel = f"{folder}/{candidate}"
            if rel not in reserved and not (self._root / folder / candidate).exists():
                reserved.add(rel)
                return self._root / folder / candidate, rel
            candidate = f"{stem}-{counter}.md"
            counter += 1

    def write_nodes(self, nodes: list[NodeDocument]) -> list[WrittenNode]:
        """Write a set of nodes, each to its ``<folder>/<slug>--<shortid>.md``. Atomic per file.

        Returns the written nodes in input order (id + store-relative path). Edge targets are
        already resolved by the caller, so ordering here is irrelevant to linking.
        """
        reserved: set[str] = set()
        written: list[WrittenNode] = []
        for node in nodes:
            filename = node_filename(
                node_id=node.id,
                node_type=node.type,
                title=node.title,
                created_local=node.created_local,
            )
            abs_path, rel = self._reserve_path(node.folder, filename, reserved)
            self._atomic_write(abs_path, render_node(node))
            written.append(WrittenNode(node_id=node.id, store_path=rel))
        return written

    def add_edges(self, store_path: str, edges: list[NodeEdge]) -> None:
        """Materialize canonical ``edges`` onto an existing node file (ADR-030 §3 resolution).

        Atomic (temp + ``os.replace``, ADR-014) and idempotent (:func:`append_edges` skips edges
        already present). A missing file raises ``FileNotFoundError`` — the caller (review service)
        treats an un-indexed / vanished source node as unmaterializable and skips it, never crashing
        (rule 7). The DB edge row is materialized separately by re-indexing this path afterwards.
        """
        path = self._root / Path(*store_path.split("/"))
        raw_text = path.read_text(encoding="utf-8")
        self._atomic_write(path, append_edges(raw_text, edges))

    def retarget_edges(self, store_path: str, *, old_to: str, new_to: str) -> int:
        """Redirect a node file's edges from ``old_to`` to ``new_to`` (merge, ADR-030 §5). Atomic;
        returns the number retargeted (0 ⇒ no write). A missing file raises ``FileNotFoundError`` —
        the caller (merge service) skips a vanished source, never crashing (rule 7)."""
        path = self._root / Path(*store_path.split("/"))
        raw_text = path.read_text(encoding="utf-8")
        rewritten, count = retarget_edges(raw_text, old_to=old_to, new_to=new_to)
        if count:
            self._atomic_write(path, rewritten)
        return count

    def set_aliases(self, store_path: str, aliases: tuple[str, ...] | list[str]) -> None:
        """Set a node file's ``aliases:`` frontmatter line (merge alias union, ADR-030 §5). Atomic;
        a missing file raises ``FileNotFoundError`` (the caller degrades, rule 7)."""
        path = self._root / Path(*store_path.split("/"))
        raw_text = path.read_text(encoding="utf-8")
        self._atomic_write(path, upsert_frontmatter_list(raw_text, "aliases", aliases))

    def write_tombstone(
        self, store_path: str, *, node_id: str, node_type: str, survivor_id: str
    ) -> None:
        """Replace a merged node's file with its tombstone (ADR-030 §5). Atomic; keeps the same path
        so old locators resolve. A missing file is ignored (already gone — nothing to tombstone)."""
        path = self._root / Path(*store_path.split("/"))
        if not path.exists():
            return
        self._atomic_write(path, render_tombstone(
            node_id=node_id, node_type=node_type, survivor_id=survivor_id
        ))

    def remove_nodes(self, store_paths: list[str]) -> None:
        """Delete files by store-relative path (Pass-2 supersede / reorganize). Missing files are
        ignored; git history retains the content once the backup service commits the deletion."""
        for rel in store_paths:
            path = self._root / Path(*rel.split("/"))
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    @staticmethod
    def _atomic_write(path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, path)
