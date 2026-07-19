"""Pure-logic tests for diacritic folding (ADR-041) — no I/O, no mocks (08 testing policy)."""

from __future__ import annotations

from app.text import fold_diacritics


def test_romanian_letters_fold_to_ascii_base():
    assert fold_diacritics("Mădălina") == "Madalina"
    assert fold_diacritics("Ștefan") == "Stefan"
    assert fold_diacritics("Țepeș") == "Tepes"
    assert fold_diacritics("î în â") == "i in a"


def test_cedilla_variants_fold_too():
    # Legacy cedilla ş/ţ (U+015F/U+0163) as well as the modern comma-below ș/ț.
    assert fold_diacritics("ştefan") == "stefan"  # ş
    assert fold_diacritics("acţiune") == "actiune"  # ţ


def test_other_diacritics_fold_via_nfkd():
    assert fold_diacritics("café") == "cafe"
    assert fold_diacritics("naïve résumé") == "naive resume"
    assert fold_diacritics("Zürich") == "Zurich"


def test_ascii_and_empty_are_unchanged():
    assert fold_diacritics("plain ascii 123") == "plain ascii 123"
    assert fold_diacritics("") == ""


def test_folding_is_idempotent():
    once = fold_diacritics("Mădălina Cole")
    assert fold_diacritics(once) == once == "Madalina Cole"
