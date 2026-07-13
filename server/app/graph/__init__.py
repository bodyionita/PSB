"""Graph domain (M3, ADR-026/030): the node writer, the derived ``similar``-edge recompute, and
its store.

The node writer (:mod:`~app.graph.node_writer`) is the single filesystem writer of typed node
files; :class:`~app.graph.service.DerivedEdgeGraph` recomputes ``edges(origin='derived')`` from
``nodes.embedding`` cosine — **DB-only**, no file rendering (the pre-pivot ``sb:related`` block and
``note_links`` table were deleted by ADR-026). Canonical edges are materialized separately, from
node frontmatter, by the indexer.
"""
