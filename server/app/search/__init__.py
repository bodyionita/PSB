"""Search domain (M2): note-grouped pgvector retrieval + read-only note preview (03-api §Search,
04 §4, ADR-022/023). The :class:`~app.search.service.SearchService` embeds the query with the
``search_query:`` prefix and ranks notes by their best chunk; M3 chat retrieval reuses it."""
