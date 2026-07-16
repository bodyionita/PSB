"""Nightly all-source dedup sweep (M6 task 5, ADR-049).

Recently-ingested **content** nodes that read as near-duplicates file a ``dedup-proposal`` review
item the user resolves with merge / keep / link. :mod:`app.dedup.store` is the candidate SQL +
re-file guard + survivor-degree reads; :mod:`app.dedup.sweep` is the :class:`DedupSweepService`
orchestration (watermark → candidates → canonicalize/dedup → re-file guard → enqueue).
"""
