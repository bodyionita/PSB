"""Pure chunking + note-text stripping (02-data-model §4, ADR-023).

The chunker is deliberately pure (no I/O, no settings, no provider calls) so it is unit-tested
with no mocks. Before a note is **chunked / embedded**, both the YAML **frontmatter** and the
machine-managed **``sb:related``** block are stripped, so a note's embedded identity is its
**human content only**. (The two strippers are exposed separately because the indexer's
``content_hash`` uses only ``strip_related_block`` — the hash keeps frontmatter so tag/plane
edits still trigger a reindex, and drops only the machine block; see 02-data-model §3 + ADR-023.)

  * the YAML **frontmatter** block, and
  * the machine-managed **``sb:related``** block ([ADR-023]) — stripping it from the hash is what
    keeps the graph's own writes from re-triggering a reindex (the feedback-loop fix).

Splitting policy (02 §4): split on **headings**, then **paragraphs**, targeting ``CHUNK_SIZE``
characters; a single over-long paragraph is **hard-split** with ``CHUNK_OVERLAP`` characters of
overlap between consecutive pieces (overlap applies only to hard splits, so nothing is lost at an
arbitrary mid-content cut).

The asymmetric ``search_document:`` / ``search_query:`` nomic prefixes ([ADR-022]) are **not**
applied here — they are an embed-time concern owned by the indexer / search layer, not the
chunker (which also feeds hashing). Chunks are returned as raw note text.
"""

from __future__ import annotations

import re

# Delimiters of the machine-owned semantic-relatedness block ([ADR-023]). Canonical here; the
# graph renderer (later M2 task) writes the same markers. The region between them (inclusive) is
# regenerated wholesale, so it is excluded from a note's identity.
RELATED_BLOCK_START = "<!-- sb:related:start -->"
RELATED_BLOCK_END = "<!-- sb:related:end -->"

_RELATED_BLOCK_RE = re.compile(
    re.escape(RELATED_BLOCK_START) + ".*?" + re.escape(RELATED_BLOCK_END),
    re.DOTALL,
)

# A YAML frontmatter block: `---` on the first line, up to the next `---` on its own line.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(?P<body>.*?\n)?---[ \t]*(?:\n|\Z)", re.DOTALL)

# An ATX markdown heading line (`#` … `######` followed by space).
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S")
# A fenced code-block delimiter line (``` or ~~~). Headings inside a fence are not headings.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# Paragraph separator: one or more blank lines.
_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n\s*")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split leading YAML frontmatter from the body.

    Returns ``(frontmatter_inner, body)`` where ``frontmatter_inner`` is the YAML between the
    ``---`` fences (``None`` if the note has no frontmatter) and ``body`` is everything after.
    Newlines are normalized to LF first, so a CRLF vault file (git on Windows) strips too.
    """
    text = _normalize_newlines(text)
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    inner = match.group("body") or ""
    return inner, text[match.end() :]


def strip_related_block(text: str) -> str:
    """Remove the machine-managed ``sb:related`` block ([ADR-023]), if present.

    Newlines are normalized to LF first so the delimiters match on a CRLF vault file.
    """
    return _RELATED_BLOCK_RE.sub("", _normalize_newlines(text))


def chunk_note(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Full note text → retrieval chunks: strip frontmatter + the ``sb:related`` block, then chunk.

    This is what the indexer feeds to the embedder (02 §4). ``notes.embedding`` is later the
    mean-pool of these chunks' vectors ([ADR-023]).
    """
    _, body = split_frontmatter(text)  # normalizes newlines internally
    body = strip_related_block(body)
    return chunk_text(body, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def chunk_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split already-cleaned body text into chunks (headings → paragraphs → hard split).

    Headings are hard boundaries (a section never merges across a heading). A section that fits
    in ``chunk_size`` is one chunk; an over-long section is packed by paragraph, and an over-long
    single paragraph is hard-split with ``chunk_overlap`` overlap.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if chunk_overlap < 0:
        raise ValueError(f"chunk_overlap must be non-negative, got {chunk_overlap}")
    chunks: list[str] = []
    for section in _split_on_headings(_normalize_newlines(text)):
        section = section.strip()
        if not section:
            continue
        if len(section) <= chunk_size:
            chunks.append(section)
        else:
            chunks.extend(_pack_paragraphs(section, chunk_size, chunk_overlap))
    return chunks


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_on_headings(text: str) -> list[str]:
    """Split ``text`` so each ATX heading starts a new section; keep any preamble as its own.

    Scans line by line so headings inside fenced code blocks (a ``#`` comment) are not treated
    as boundaries. A heading on the very first line starts no new section (it heads the first one).
    """
    starts: list[int] = []
    offset = 0
    in_fence = False
    lines = text.split("\n")
    for index, line in enumerate(lines):
        line_len = len(line) + (1 if index < len(lines) - 1 else 0)  # re-add the split '\n'
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and offset > 0 and _HEADING_RE.match(line):
            starts.append(offset)
        offset += line_len
    if not starts:
        return [text]
    bounds = [0, *starts]
    ends = [*starts, len(text)]
    return [text[a:b] for a, b in zip(bounds, ends, strict=True)]


def _pack_paragraphs(section: str, size: int, overlap: int) -> list[str]:
    """Greedily pack paragraphs up to ``size``; hard-split any single paragraph that exceeds it."""
    chunks: list[str] = []
    current = ""
    for para in _PARAGRAPH_SPLIT_RE.split(section):
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(para, size, overlap))
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= size:
            current = candidate
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """Slice an over-long string into ``size`` windows overlapping by ``overlap`` characters."""
    step = max(1, size - overlap) if overlap < size else size
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return chunks
