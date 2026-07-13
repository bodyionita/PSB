"""Pure sb:related renderer tests (ADR-023) — no I/O, no mocks.

Covers the block shape (path-target + title-alias wikilinks, highest score first), placement at
the end of the body, idempotency (the churn gate depends on it), stripping a stale block when a
note loses its neighbours, and that the co-capture ``## Related`` section is left untouched.
"""

from __future__ import annotations

from app.graph.renderer import apply_related_block, render_related_block
from app.graph.store import RelatedLink
from app.indexing.chunking import RELATED_BLOCK_END, RELATED_BLOCK_START, strip_related_block

_BODY = """---
id: cap-1
plane: Ideas
tags: [thinking]
---

# A bright idea

The body of a bright idea worth remembering.
"""

_LINK_A = RelatedLink(
    note_id="id-a",
    vault_path="Ideas/2026-07-12 Braindan — second brain app.md",
    title="Braindan — second brain app",
    score=0.81,
)
_LINK_B = RelatedLink(
    note_id="id-b", vault_path="Health/2026-07-11 Morning run.md", title="Morning run", score=0.62
)


_LINK_A_WIKILINK = "- [[Ideas/2026-07-12 Braindan — second brain app|Braindan — second brain app]]"


def test_render_block_shape_path_target_and_title_alias():
    block = render_related_block([_LINK_A])
    assert block.splitlines() == [
        RELATED_BLOCK_START,
        "## Related notes",
        _LINK_A_WIKILINK,
        RELATED_BLOCK_END,
    ]


def test_render_block_orders_as_given_and_drops_md_extension():
    block = render_related_block([_LINK_A, _LINK_B])
    lines = block.splitlines()
    assert lines[2] == _LINK_A_WIKILINK
    assert lines[3] == "- [[Health/2026-07-11 Morning run|Morning run]]"


def test_render_block_falls_back_to_basename_when_no_title():
    link = RelatedLink(note_id="x", vault_path="Ideas/no-title-note.md", title=None, score=0.7)
    assert "[[Ideas/no-title-note|no-title-note]]" in render_related_block([link])


def test_no_links_renders_empty_block():
    assert render_related_block([]) == ""


def test_apply_appends_block_at_end_of_body():
    result = apply_related_block(_BODY, [_LINK_A])
    assert result.startswith(_BODY.rstrip())
    assert result.endswith(RELATED_BLOCK_END + "\n")
    # A blank line separates the human body from the machine block.
    assert "\n\n" + RELATED_BLOCK_START in result


def test_apply_is_idempotent():
    once = apply_related_block(_BODY, [_LINK_A, _LINK_B])
    twice = apply_related_block(once, [_LINK_A, _LINK_B])
    assert once == twice


def test_apply_replaces_an_existing_block_rather_than_stacking():
    once = apply_related_block(_BODY, [_LINK_A])
    updated = apply_related_block(once, [_LINK_B])
    assert updated.count(RELATED_BLOCK_START) == 1
    assert "Morning run" in updated
    assert "second brain app" not in updated


def test_apply_with_no_links_strips_a_stale_block():
    with_block = apply_related_block(_BODY, [_LINK_A])
    stripped = apply_related_block(with_block, [])
    assert RELATED_BLOCK_START not in stripped
    assert stripped == _BODY.rstrip() + "\n"


def test_apply_leaves_co_capture_related_section_untouched():
    body = _BODY + "\n## Related\n- [[Ideas/sibling-note]]\n"
    result = apply_related_block(body, [_LINK_A])
    # The human co-capture section survives; only the delimited machine block is added.
    assert "## Related\n- [[Ideas/sibling-note]]" in result
    assert strip_related_block(result).count("## Related notes") == 0


def test_apply_normalizes_crlf_to_lf():
    crlf = _BODY.replace("\n", "\r\n")
    result = apply_related_block(crlf, [_LINK_A])
    assert "\r" not in result
