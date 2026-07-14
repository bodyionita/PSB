"""Cited-only ``[n]`` parsing + renumbering for chat answers (04-pipelines §5, ADR-025).

The chat model is handed numbered context items ``[1..k]`` (retrieval order) and asked to cite the
ones it uses as ``[n]``. Before the answer is returned/persisted we keep **only the cited nodes**
and renumber them ``[1..m]`` in the order they first appear in the prose, so the reader's ``[1]`` is
the first source card. Out-of-range markers (a model citing ``[9]`` when only 3 items were given,
or ``[0]``) are dropped, never an error (04 §5).

Pure + unit-tested — no I/O. The service (``chat/service.py``) owns retrieval and the model call.
"""

from __future__ import annotations

import re

# A citation marker: a bracketed positive integer, e.g. ``[1]``. Bare/negative/other bracketed text
# (``[note]``) is left untouched — only ``[digits]`` is treated as a citation.
_CITATION = re.compile(r"\[(\d+)\]")


def renumber_citations[T](text: str, hits: list[T]) -> tuple[str, list[T]]:
    """Keep only the ``hits`` the answer cites and renumber them ``[1..m]`` by first appearance.

    ``hits`` is the retrieval-ordered context (``hits[n-1]`` is the item the model saw as ``[n]``).
    Returns ``(rewritten_text, cited_hits)`` where every surviving ``[n]`` in the text has been
    remapped to its new 1-based position and ``cited_hits`` is aligned to that new numbering. A
    marker outside ``1..len(hits)`` is removed from the text and contributes no source (never
    raises). A repeated citation reuses its assigned number.
    """
    k = len(hits)
    mapping: dict[int, int] = {}  # old 1-based index → new 1-based index
    order: list[int] = []  # old indices, in first-appearance order

    def _replace(match: re.Match[str]) -> str:
        old = int(match.group(1))
        if old < 1 or old > k:
            return ""  # out-of-range citation dropped (04 §5: never an error)
        if old not in mapping:
            order.append(old)
            mapping[old] = len(order)
        return f"[{mapping[old]}]"

    rewritten = _CITATION.sub(_replace, text)
    cited = [hits[old - 1] for old in order]
    return rewritten, cited
