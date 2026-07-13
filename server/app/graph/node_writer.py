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
