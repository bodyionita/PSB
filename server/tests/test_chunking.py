"""Pure chunker tests (02-data-model §4, ADR-023) — no mocks, no I/O.

Small chunk sizes keep the packing/hard-split behaviour easy to assert; the real defaults are
CHUNK_SIZE=1200 / CHUNK_OVERLAP=200 (config).
"""

from __future__ import annotations

import pytest

from app.indexing.chunking import (
    RELATED_BLOCK_END,
    RELATED_BLOCK_START,
    chunk_note,
    chunk_text,
    split_frontmatter,
    strip_related_block,
)

# --- frontmatter stripping --------------------------------------------------------------


def test_split_frontmatter_separates_yaml_from_body():
    text = "---\nid: abc\nplane: ideas\n---\n# Title\n\nbody text"
    inner, body = split_frontmatter(text)
    assert inner == "id: abc\nplane: ideas\n"
    assert body == "# Title\n\nbody text"


def test_split_frontmatter_none_when_absent():
    text = "# Title\n\nbody with a --- horizontal rule\n\n---\n\nmore"
    inner, body = split_frontmatter(text)
    assert inner is None
    assert body == text  # a mid-body `---` is never mistaken for frontmatter


def test_split_frontmatter_handles_empty_block():
    inner, body = split_frontmatter("---\n---\nbody")
    assert inner == ""
    assert body == "body"


# --- sb:related block stripping (ADR-023) -----------------------------------------------


def test_strip_related_block_removes_delimited_region():
    body = (
        "# Note\n\nhuman content\n\n"
        f"{RELATED_BLOCK_START}\n## Related notes\n- [[Ideas/Foo|Foo]]\n{RELATED_BLOCK_END}\n"
    )
    stripped = strip_related_block(body)
    assert "Related notes" not in stripped
    assert "human content" in stripped
    assert RELATED_BLOCK_START not in stripped and RELATED_BLOCK_END not in stripped


def test_strip_related_block_noop_without_block():
    body = "# Note\n\njust content"
    assert strip_related_block(body) == body


# --- chunk_note: the indexer entry point ------------------------------------------------


def test_chunk_note_strips_frontmatter_and_related_block():
    text = (
        "---\nid: abc\ntags: [x]\n---\n"
        "# Title\n\nthe body\n\n"
        f"{RELATED_BLOCK_START}\n## Related notes\n- [[Ideas/Foo|Foo]]\n{RELATED_BLOCK_END}\n"
    )
    chunks = chunk_note(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == ["# Title\n\nthe body"]
    # neither frontmatter keys nor the machine block survive into the embedded text
    assert all("id: abc" not in c and "Related notes" not in c for c in chunks)


# --- chunk_text: splitting policy -------------------------------------------------------


def test_small_note_is_a_single_chunk():
    assert chunk_text("# Title\n\nshort body", chunk_size=1200, chunk_overlap=200) == [
        "# Title\n\nshort body"
    ]


def test_headings_are_hard_boundaries():
    text = "preamble\n\n# First\n\nalpha\n\n## Second\n\nbeta"
    chunks = chunk_text(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == ["preamble", "# First\n\nalpha", "## Second\n\nbeta"]


def test_oversized_section_packs_by_paragraph_under_the_limit():
    # Three 10-char paragraphs in one section (34 chars) with size 20 ⇒ each paragraph its own
    # chunk (packing two would exceed the limit). No overlap between paragraph groups.
    section = "aaaaaaaaaa\n\nbbbbbbbbbb\n\ncccccccccc"
    chunks = chunk_text(section, chunk_size=20, chunk_overlap=5)
    assert chunks == ["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"]
    assert all(len(c) <= 20 for c in chunks)


def test_two_small_paragraphs_pack_together():
    section = "aaaa\n\nbbbb\n\n" + "x" * 40  # first two pack; the 40-char para hard-splits
    chunks = chunk_text(section, chunk_size=20, chunk_overlap=5)
    assert chunks[0] == "aaaa\n\nbbbb"


def test_long_paragraph_hard_splits_with_overlap():
    para = "".join(chr(ord("a") + (i % 26)) for i in range(50))  # 50 chars, no blank lines
    chunks = chunk_text(para, chunk_size=20, chunk_overlap=5)
    # windows of 20 stepping by 15 (size - overlap): 0-20, 15-35, 30-50
    assert chunks == [para[0:20], para[15:35], para[30:50]]
    # consecutive chunks share exactly `overlap` characters
    assert chunks[0][-5:] == chunks[1][:5]
    assert chunks[1][-5:] == chunks[2][:5]
    assert all(len(c) <= 20 for c in chunks)


def test_hard_split_covers_all_content_with_overlap():
    # Sliding windows (step = size - overlap) lose nothing: dropping each later window's
    # `overlap` prefix stitches the original text back exactly, tail included.
    text = "".join(chr(ord("a") + i % 26) for i in range(41))
    chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
    assert chunks[0] + "".join(c[5:] for c in chunks[1:]) == text
    assert chunks[-1].endswith(text[-1])


def test_empty_or_whitespace_yields_no_chunks():
    assert chunk_text("", chunk_size=1200, chunk_overlap=200) == []
    assert chunk_text("   \n\n\t\n", chunk_size=1200, chunk_overlap=200) == []
    assert chunk_note("---\nid: x\n---\n", chunk_size=1200, chunk_overlap=200) == []


def test_crlf_is_normalized():
    chunks = chunk_text("# T\r\n\r\nbody", chunk_size=1200, chunk_overlap=200)
    assert chunks == ["# T\n\nbody"]


def test_overlap_at_least_size_degrades_to_no_overlap():
    # A pathological config (overlap >= size) must not stall or produce empty windows.
    text = "q" * 30
    chunks = chunk_text(text, chunk_size=10, chunk_overlap=10)
    assert chunks == ["q" * 10, "q" * 10, "q" * 10]


def test_headings_inside_a_code_fence_are_not_boundaries():
    # A `#` line inside a fenced code block is a comment, not a heading — the block must not split.
    text = "# Real\n\n```python\n# not a heading\nx = 1\n```\n\nafter"
    chunks = chunk_text(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == [text]  # one section; the fence's `#` line did not cut it
    assert any("# not a heading" in c and "x = 1" in c for c in chunks)


def test_heading_after_a_closed_fence_still_splits():
    text = "```\n# fenced\n```\n\n# Real heading\n\nbody"
    chunks = chunk_text(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == ["```\n# fenced\n```", "# Real heading\n\nbody"]


def test_invalid_chunk_params_raise():
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=10, chunk_overlap=-1)


def test_primitives_normalize_crlf():
    # split_frontmatter / strip_related_block are called directly by the indexer's hash builder
    # on raw vault text, which may be CRLF (git on Windows) — they must still strip.
    inner, body = split_frontmatter("---\r\nid: x\r\n---\r\n# T\r\n\r\nbody")
    assert inner == "id: x\n"
    assert body == "# T\n\nbody"

    crlf = f"a\r\n\r\n{RELATED_BLOCK_START}\r\n- [[x]]\r\n{RELATED_BLOCK_END}\r\nb"
    assert RELATED_BLOCK_START not in strip_related_block(crlf)
