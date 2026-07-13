"""Pure rendering of the machine-managed ``sb:related`` block (ADR-023).

No I/O, no settings — unit-tested with no mocks. The block is a delimited ``## Related notes``
list of path-target + title-alias ``[[wikilinks]]`` placed at the **end** of the note body, so
it shows in Obsidian's graph view. It is strictly distinct from the co-capture ``related:``
frontmatter + human ``## Related`` section, which this never touches.

``apply_related_block`` is **idempotent**: it strips any existing block (and the residual blank
lines it leaves) before appending the freshly-rendered one, so running the recompute twice on an
unchanged graph reproduces byte-identical output — which is what makes the caller's churn gate
(rewrite the file only when the content changed) actually hold.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..indexing.chunking import (
    RELATED_BLOCK_END,
    RELATED_BLOCK_START,
    strip_related_block,
)
from .store import RelatedLink

# The heading that opens the block body (ADR-023). Kept adjacent to the delimiters it lives
# between so the render shape has a single source of truth.
_RELATED_HEADING = "## Related notes"


def _wikilink(link: RelatedLink) -> str:
    """A path-target + title-alias wikilink: ``[[Plane/2026-... Title|Title]]`` (ADR-023).

    The path target (vault path without ``.md``) resolves the exact note even when two notes
    across planes share a title; the alias is the human-readable title (falling back to the
    file's basename when a note has none).
    """
    target = link.vault_path[:-3] if link.vault_path.endswith(".md") else link.vault_path
    alias = link.title or target.rsplit("/", 1)[-1]
    return f"[[{target}|{alias}]]"


def render_related_block(links: Sequence[RelatedLink]) -> str:
    """Render the delimited ``sb:related`` block for a note's neighbours (highest score first).

    Returns the empty string when there are no neighbours — a note with no links carries no
    block at all (the caller strips any stale one).
    """
    if not links:
        return ""
    lines = [RELATED_BLOCK_START, _RELATED_HEADING]
    lines.extend(f"- {_wikilink(link)}" for link in links)
    lines.append(RELATED_BLOCK_END)
    return "\n".join(lines)


def apply_related_block(raw_text: str, links: Sequence[RelatedLink]) -> str:
    """Return ``raw_text`` with its ``sb:related`` block replaced by the one for ``links``.

    Newlines are normalized to LF (the vault invariant, ADR-014 ``.gitattributes`` — done by
    ``strip_related_block``). Any existing block is removed first, then the note ends with exactly
    one trailing newline; a non-empty block is appended after one blank line. Idempotent — see the
    module docstring.
    """
    # `base` is LF, block-free, and trailing-whitespace-trimmed, terminated with a single `\n`.
    # A note that was previously CRLF / lacked a trailing newline is thus normalized on its first
    # recompute even if it carries no block; that is a one-time write (stable on every run after),
    # so the zero-churn-on-a-stable-graph contract still holds.
    base = strip_related_block(raw_text).rstrip()
    block = render_related_block(links)
    if not block:
        return f"{base}\n"
    return f"{base}\n\n{block}\n"
