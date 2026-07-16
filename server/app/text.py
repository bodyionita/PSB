"""Text normalization utilities shared across the write + match paths (ADR-041).

Currently one utility: :func:`fold_diacritics`, the single authority for stripping diacritics to
ASCII. It is applied at the ``NodeWriter`` write chokepoint (so nothing written to the graph store
carries a diacritic — filename slug, title, aliases, disambig, tags, body) and on the matching side
(``normalize_alias`` + the tag slug) so retrieval and accretion are diacritic-insensitive and
consistent with the already-folded stored forms. The **raw** capture is never folded — it is the
never-lose source of truth (CLAUDE.md rule 2) and what ``reprocess-all-from-raw`` replays.

Pure, no I/O — unit-tested with no mocks.
"""

from __future__ import annotations

import unicodedata

# Romanian letters mapped defensively before the NFKD pass. NFKD *does* decompose the modern
# comma-below ``ș``/``ț`` (U+0219/U+021B) and the cedilla variants (U+015F/U+0163), but mapping
# them explicitly guards against any font/normalization form that slips a precomposed glyph
# through, and documents the intended target letters (ADR-041 §1).
_ROMANIAN_FOLD = str.maketrans(
    {
        "ă": "a",
        "Ă": "A",
        "â": "a",
        "Â": "A",
        "î": "i",
        "Î": "I",
        "ș": "s",
        "Ș": "S",
        "ț": "t",
        "Ț": "T",
        "ş": "s",
        "Ş": "S",  # cedilla variants (legacy Unicode)
        "ţ": "t",
        "Ţ": "T",
    }
)


def fold_diacritics(text: str) -> str:
    """Fold a string to its ASCII base letters: Romanian map + Unicode NFKD, combining marks
    stripped (ADR-041). ``"Mădălina"`` → ``"Madalina"``, ``"Ștefan"`` → ``"Stefan"``. Idempotent
    (folding folded text is a no-op) and safe on already-ASCII text. Empty in ⇒ empty out."""
    if not text:
        return text
    mapped = text.translate(_ROMANIAN_FOLD)
    decomposed = unicodedata.normalize("NFKD", mapped)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
