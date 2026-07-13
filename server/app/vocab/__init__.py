"""Vocabulary governance (ADR-027 / ADR-035, M3 task 7).

The propose → approve → consolidate machinery for the typed node/edge vocabulary:

  * :mod:`store` — approved additions persisted in ``app_settings`` (the mutable half of the
    vocabulary; the seeds live in :class:`~app.config.Settings`).
  * :mod:`service` — the **effective vocabulary** (seeds ∪ approved additions) every writer reads,
    plus :class:`~app.vocab.service.VocabularyService` (``GET /types`` listing + the shared
    approve/reject choke point behind ``PUT /settings/vocabulary`` and the Review queue).
  * :mod:`consolidation` — the ``vocab-consolidation`` job an approval opens (ADR-035: edges
    propose→apply, nodes propose-only).
"""
