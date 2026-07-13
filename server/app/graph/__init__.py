"""Semantic relatedness graph (M2, ADR-023).

Recomputes the ``note_links`` table (top-K over ``notes.embedding`` cosine above a floor) and
renders the machine-managed ``sb:related`` ``## Related notes`` wikilink block into each note
body so the topical graph shows in Obsidian's graph view — distinct from the co-capture
``related:`` frontmatter. Nightly-only + churn-gated; the real-time capture path never touches it.
"""
