"""Note rendering + vault writing (02-data-model §2, ADR-005/014).

Two layers:
  * Pure helpers — filename sanitisation, frontmatter/body rendering — unit-tested with no I/O.
  * :class:`NoteWriter` — the only place capture notes touch the filesystem. Writes are atomic
    (temp + ``os.replace``, ADR-014) and collision-safe (numeric suffix). Git is NOT this
    class's concern: it only writes/removes files; the ``VaultBackupService`` commits them.

Vault-relative paths are always ``/``-separated regardless of OS (CLAUDE.md conventions).
Blocking filesystem work is synchronous here; the pipeline calls it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .organizer import OrganizerNote

# Characters illegal in Windows filenames (superset of POSIX needs) + control chars.
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")

_MAX_TITLE_LEN = 120  # keep filenames comfortably under path limits


def sanitize_title(title: str) -> str:
    """Filesystem-safe note title: strip illegal chars, collapse whitespace, bound length.

    Never returns empty (falls back to ``"Untitled"``) and never ends in a dot/space (Windows).
    """
    cleaned = _ILLEGAL_FS.sub(" ", title)
    cleaned = _WS.sub(" ", cleaned).strip()
    cleaned = cleaned[:_MAX_TITLE_LEN].strip()
    cleaned = cleaned.rstrip(". ").strip()
    return cleaned or "Untitled"


def note_filename(created_local: datetime, title: str) -> str:
    """``<YYYY-MM-DD> <Sanitized Title>.md`` — date from the note's local created time."""
    return f"{created_local:%Y-%m-%d} {sanitize_title(title)}.md"


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when needed (special chars); escape embedded quotes."""
    if value and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def render_frontmatter(
    *,
    note_id: str,
    created_local: datetime,
    source: str,
    source_ref: str | None,
    plane: str,
    planes: tuple[str, ...],
    tags: tuple[str, ...],
    related: tuple[str, ...],
) -> str:
    """Render the frontmatter block (02 §2). ``source_ref`` is omitted when absent."""
    lines = ["---", f"id: {note_id}", f"created: {created_local.isoformat()}", f"source: {source}"]
    if source_ref:
        lines.append(f"source_ref: {_yaml_scalar(source_ref)}")
    lines.append(f"plane: {_yaml_scalar(plane)}")
    lines.append(f"planes: {_yaml_list(planes)}")
    lines.append(f"tags: {_yaml_list(tags)}")
    lines.append(f"related: {_yaml_list(related)}")
    lines.append("---")
    return "\n".join(lines)


def _wikilink(vault_path: str) -> str:
    """Obsidian-style link to a sibling note: path without the .md extension."""
    stem = vault_path[:-3] if vault_path.endswith(".md") else vault_path
    return f"[[{stem}]]"


def render_note(
    note: OrganizerNote,
    *,
    note_id: str,
    created_local: datetime,
    source: str,
    source_ref: str | None,
    related: tuple[str, ...],
) -> str:
    """Full note file contents: frontmatter + H1 title + body + optional Related section."""
    parts = [
        render_frontmatter(
            note_id=note_id,
            created_local=created_local,
            source=source,
            source_ref=source_ref,
            plane=note.plane,
            planes=note.planes,
            tags=note.tags,
            related=related,
        ),
        "",
        f"# {note.title}",
        "",
        note.body.strip(),
    ]
    if related:
        parts.extend(["", "## Related", *[f"- {_wikilink(p)}" for p in related]])
    return "\n".join(parts).rstrip() + "\n"


@dataclass(frozen=True)
class WrittenNote:
    vault_path: str  # /-separated, vault-relative
    title: str


class NoteWriter:
    """Writes capture notes into the vault filesystem. Atomic + collision-safe."""

    def __init__(self, vault_path: str) -> None:
        self._vault_root = Path(vault_path)

    def _reserve_path(self, plane: str, filename: str, reserved: set[str]) -> tuple[Path, str]:
        """Resolve a non-colliding target, honouring both on-disk files and sibling reservations.

        Returns the absolute path and the ``/``-separated vault-relative path.
        """
        folder = self._vault_root / plane
        stem = filename[:-3]  # drop ".md"
        candidate = filename
        counter = 2
        while True:
            rel = f"{plane}/{candidate}"
            if rel not in reserved and not (folder / candidate).exists():
                reserved.add(rel)
                return folder / candidate, rel
            candidate = f"{stem} {counter}.md"
            counter += 1

    def write_notes(
        self,
        notes: list[OrganizerNote],
        *,
        capture_id: str,
        created_local: datetime,
        source: str,
        source_ref: str | None = None,
    ) -> list[str]:
        """Write a sibling set of notes, cross-linked via ``related`` + ``[[wikilinks]]``.

        All notes from one capture share ``id`` (the capture id) and cross-reference each other.
        Returns the vault-relative paths in note order. Atomic per file.
        """
        reserved: set[str] = set()
        planned: list[tuple[OrganizerNote, Path, str]] = []
        for note in notes:
            abs_path, rel = self._reserve_path(
                note.plane, note_filename(created_local, note.title), reserved
            )
            planned.append((note, abs_path, rel))

        all_rel = [rel for _, _, rel in planned]
        written: list[str] = []
        for note, abs_path, rel in planned:
            related = tuple(p for p in all_rel if p != rel)
            contents = render_note(
                note,
                note_id=capture_id,
                created_local=created_local,
                source=source,
                source_ref=source_ref,
                related=related,
            )
            self._atomic_write(abs_path, contents)
            written.append(rel)
        return written

    def remove_notes(self, vault_paths: list[str]) -> None:
        """Delete files by vault-relative path (Pass-2 supersede). Missing files are ignored;
        git history retains the content once the backup service commits the deletion (ADR-019)."""
        for rel in vault_paths:
            path = self._vault_root / Path(*rel.split("/"))
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
