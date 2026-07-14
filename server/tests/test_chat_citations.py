"""Cited-only [n] renumbering tests (M4 task 3, 04-pipelines §5). Pure — no I/O."""

from __future__ import annotations

from app.chat.citations import renumber_citations


def test_keeps_only_cited_and_renumbers_by_first_appearance():
    text = "You raised prices [3] after the chat with Ana [1]."
    hits = ["h1", "h2", "h3"]
    new_text, cited = renumber_citations(text, hits)

    # [3] appears first → becomes [1]; [1] appears second → becomes [2]. h2 was never cited.
    assert new_text == "You raised prices [1] after the chat with Ana [2]."
    assert cited == ["h3", "h1"]


def test_repeated_citation_reuses_its_number():
    text = "A [2] then B [2] and C [1]."
    new_text, cited = renumber_citations(text, ["h1", "h2"])
    assert new_text == "A [1] then B [1] and C [2]."
    assert cited == ["h2", "h1"]


def test_out_of_range_citations_are_dropped_not_errored():
    text = "known [1], too big [9], zero [0]."
    new_text, cited = renumber_citations(text, ["h1"])
    # Only [1] is valid; [9]/[0] are removed, no exception.
    assert new_text == "known [1], too big , zero ."
    assert cited == ["h1"]


def test_no_citations_yields_empty_sources_and_unchanged_text():
    text = "A general answer with no memory references."
    new_text, cited = renumber_citations(text, ["h1", "h2"])
    assert new_text == text
    assert cited == []


def test_non_citation_brackets_are_left_alone():
    text = "See [note] and array[0] — but cite [1]."
    new_text, cited = renumber_citations(text, ["h1"])
    # `[note]` isn't digits; `array[0]` is [0] → out of range → dropped. Only [1] survives.
    assert new_text == "See [note] and array — but cite [1]."
    assert cited == ["h1"]


def test_empty_hits_drops_all_citations():
    new_text, cited = renumber_citations("nothing here [1] to cite [2]", [])
    assert new_text == "nothing here  to cite "
    assert cited == []
