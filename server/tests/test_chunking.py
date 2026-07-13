"""Pure chunker tests (02-data-model §4, ADR-026) — no mocks, no I/O.

Small chunk sizes keep the packing/hard-split behaviour easy to assert; the real defaults are
CHUNK_SIZE=1200 / CHUNK_OVERLAP=200 (config). The ``sb:related`` / co-capture stripping machinery
was deleted by ADR-026 — a node's edges live in frontmatter, so only frontmatter is stripped.
"""

from __future__ import annotations

import pytest

from app.indexing.chunking import chunk_node, chunk_text, split_frontmatter

# --- frontmatter stripping --------------------------------------------------------------


def test_split_frontmatter_separates_yaml_from_body():
    text = "---\nid: abc\ntype: memory\n---\n# Title\n\nbody text"
    inner, body = split_frontmatter(text)
    assert inner == "id: abc\ntype: memory\n"
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


# --- chunk_node: the indexer entry point ------------------------------------------------


def test_chunk_node_strips_frontmatter_only():
    text = "---\nid: abc\ntype: memory\ntags: [x]\n---\n# Title\n\nthe body"
    chunks = chunk_node(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == ["# Title\n\nthe body"]
    assert all("id: abc" not in c for c in chunks)  # frontmatter never survives into the embed


def test_chunk_node_keeps_a_prose_related_section():
    # There is no co-capture stripping anymore — a "## Related" prose section is just content.
    text = "---\nid: x\n---\n# Note\n\ncontent\n\n## Related\n\nsee last week's chat"
    chunks = chunk_node(text, chunk_size=1200, chunk_overlap=200)
    assert any("Related" in c for c in chunks)


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
    assert chunks == [para[0:20], para[15:35], para[30:50]]
    assert chunks[0][-5:] == chunks[1][:5]
    assert chunks[1][-5:] == chunks[2][:5]
    assert all(len(c) <= 20 for c in chunks)


def test_hard_split_covers_all_content_with_overlap():
    text = "".join(chr(ord("a") + i % 26) for i in range(41))
    chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
    assert chunks[0] + "".join(c[5:] for c in chunks[1:]) == text
    assert chunks[-1].endswith(text[-1])


def test_empty_or_whitespace_yields_no_chunks():
    assert chunk_text("", chunk_size=1200, chunk_overlap=200) == []
    assert chunk_text("   \n\n\t\n", chunk_size=1200, chunk_overlap=200) == []
    assert chunk_node("---\nid: x\n---\n", chunk_size=1200, chunk_overlap=200) == []


def test_crlf_is_normalized():
    chunks = chunk_text("# T\r\n\r\nbody", chunk_size=1200, chunk_overlap=200)
    assert chunks == ["# T\n\nbody"]


def test_overlap_at_least_size_degrades_to_no_overlap():
    text = "q" * 30
    chunks = chunk_text(text, chunk_size=10, chunk_overlap=10)
    assert chunks == ["q" * 10, "q" * 10, "q" * 10]


def test_headings_inside_a_code_fence_are_not_boundaries():
    text = "# Real\n\n```python\n# not a heading\nx = 1\n```\n\nafter"
    chunks = chunk_text(text, chunk_size=1200, chunk_overlap=200)
    assert chunks == [text]
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


def test_split_frontmatter_normalizes_crlf():
    inner, body = split_frontmatter("---\r\nid: x\r\n---\r\n# T\r\n\r\nbody")
    assert inner == "id: x\n"
    assert body == "# T\n\nbody"
