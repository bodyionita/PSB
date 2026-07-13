"""Frontmatter parsing + note metadata extraction (02-data-model §2, §3).

Pure, no I/O — unit-tested with no mocks. The indexer needs a note's frontmatter *parsed*
(plane / planes / tags / source / source_ref / created) to fill the ``notes`` row, whereas the
chunker only needs it *stripped*. Both read the same ``split_frontmatter`` boundary.

We deliberately do **not** pull in a YAML dependency. Pipeline-written notes use the small,
controlled shape rendered by ``capture/notes.py`` (``key: scalar`` and ``key: [inline, list]``),
and user-authored notes are read leniently: anything this parser can't understand is ignored and
the field falls back (plane ← folder, planes ← ``[plane]``, created ← file mtime). A note's DB
identity is its ``vault_path``; frontmatter only enriches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .chunking import split_frontmatter

# An ATX H1 (`# Title`) — the note's title when present (02 §2). Skips deeper headings.
_H1_RE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
# A fenced code-block delimiter — an H1 inside a fence is not a title.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")


@dataclass(frozen=True)
class NoteMetadata:
    """Frontmatter-derived fields for a ``notes`` row (02 §3). All optional at read time."""

    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    source: str | None
    source_ref: str | None
    created: datetime | None


def parse_frontmatter(inner: str) -> dict[str, str | list[str]]:
    """Parse the YAML *inner* block into a flat ``{key: scalar | list}`` mapping.

    Understands only the shapes this project emits: ``key: scalar`` and ``key: [a, b]`` inline
    lists, both with optional double-quoting. Comment / blank lines and anything more exotic
    (nested maps, block lists) are skipped rather than guessed at.
    """
    result: dict[str, str | list[str]] = {}
    for line in inner.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Only treat a colon as the key separator when the line starts at column 0 (a top-level
        # key), so a colon inside a value doesn't split it.
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


def parse_note_metadata(
    raw_text: str, *, vault_path: str, fallback_created: datetime
) -> NoteMetadata:
    """Extract the ``notes``-row metadata from a full note file (02 §2/§3).

    ``plane`` falls back to the note's top-level folder, ``planes`` to ``[plane]``, ``title`` to
    the H1 (else the filename stem), and ``created`` to ``fallback_created`` (the file mtime).
    """
    inner, body = split_frontmatter(raw_text)
    fields = parse_frontmatter(inner) if inner is not None else {}

    plane = _as_scalar(fields.get("plane")) or _folder_of(vault_path)
    planes = _as_list(fields.get("planes"))
    if not planes:
        planes = [plane] if plane else []
    tags = _as_list(fields.get("tags"))
    title = _first_h1(body) or _stem_of(vault_path)
    created = _parse_created(_as_scalar(fields.get("created"))) or fallback_created

    return NoteMetadata(
        title=title,
        plane=plane,
        planes=planes,
        tags=tags,
        source=_as_scalar(fields.get("source")),
        source_ref=_as_scalar(fields.get("source_ref")),
        created=created,
    )


def _parse_inline_list(inner: str) -> list[str]:
    """Split a ``a, "b, c", d`` inline-list body, honouring double quotes so a quoted value's
    comma doesn't split it (a ``related:`` path may contain commas)."""
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
        elif ch == "," and not in_quote:
            token = _unquote("".join(current).strip())
            if token:
                items.append(token)
            current = []
        else:
            current.append(ch)
    token = _unquote("".join(current).strip())
    if token:
        items.append(token)
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


def _folder_of(vault_path: str) -> str | None:
    """Top-level folder of a ``/``-separated vault path = its primary plane (02 §3)."""
    head, _, tail = vault_path.partition("/")
    return head if tail else None


def _stem_of(vault_path: str) -> str:
    name = vault_path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _parse_created(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
